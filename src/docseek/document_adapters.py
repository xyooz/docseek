from __future__ import annotations

import importlib.util
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from .chunk_spool import DocumentChunkSpool
from .chunks import (
    XLSX_PROGRESS_ROW_INTERVAL,
    ChunkProgressCallback,
    DocumentChunk,
    _iter_direct_document_chunks,
    _xlsx_chunk_content,
    _xlsx_row_text,
)
from .document_types import (
    CALAMINE_CANDIDATE_EXTENSIONS,
    DIRECT_SUPPORTED_EXTENSIONS,
    FORMAT_CAPABILITIES,
    TIKA_NATIVE_CANDIDATE_EXTENSIONS,
    WPS_LOCAL_CANDIDATE_EXTENSIONS,
)
from .wps_adapter import can_convert_extension, converted_openxml


OLE_COMPOUND_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")


def _has_ole_compound_header(path: Path) -> bool:
    """Detect a legacy/renamed Office container with one bounded read."""
    try:
        with Path(path).open("rb") as handle:
            return handle.read(len(OLE_COMPOUND_MAGIC)) == OLE_COMPOUND_MAGIC
    except OSError:
        return False


class AdapterUnavailable(RuntimeError):
    """A known document format has no usable local parser on this machine."""


class DocumentAdapter(Protocol):
    name: str
    priority: int
    extensions: frozenset[str]

    def is_available(self) -> bool: ...

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]: ...


def _iter_flat_text_chunks(text: str, *, target_chars: int) -> Iterator[DocumentChunk]:
    """Bound text-only compatibility output without inventing document structure."""
    ordinal = 0
    buffer: list[str] = []
    char_count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buffer.append(stripped)
        char_count += len(stripped)
        if char_count >= target_chars:
            content = "\n".join(buffer).strip()
            if content:
                yield DocumentChunk(ordinal, f"内容块 {ordinal + 1}", content)
                ordinal += 1
            buffer = []
            char_count = 0

    content = "\n".join(buffer).strip()
    if content:
        yield DocumentChunk(ordinal, f"内容块 {ordinal + 1}", content)


def _normalize_calamine_cell(value: object) -> object:
    """Keep Calamine search text compatible with the established openpyxl path.

    python-calamine represents numeric Excel cells as floats, including cells
    whose stored value is an integer. It also exposes a date-only cell as
    ``date`` while openpyxl's data-only reader yields midnight ``datetime`` for
    the same XLSX cell. Normalize only those representation differences so the
    indexed text remains byte-for-byte compatible with the established path.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def _iter_calamine_spreadsheet_chunks(
    path: Path,
    *,
    rows_per_chunk: int,
    on_progress: ChunkProgressCallback | None = None,
) -> Iterator[DocumentChunk]:
    from python_calamine import CalamineWorkbook

    workbook = CalamineWorkbook.from_path(path)
    ordinal = 0
    try:
        for sheet_name in workbook.sheet_names:
            sheet = workbook.get_sheet_by_name(sheet_name)
            buffer: list[str] = []
            first_row = 1
            last_row = 0
            processed_row = 0
            progress_label = f"工作表 {sheet_name}"

            for row_no, row in enumerate(sheet.iter_rows(), start=1):
                processed_row = row_no
                normalized_row = tuple(_normalize_calamine_cell(value) for value in row)
                row_text = _xlsx_row_text(normalized_row)
                if row_text:
                    if not buffer:
                        first_row = row_no
                    buffer.append(row_text)
                    last_row = row_no
                if buffer and len(buffer) >= rows_per_chunk:
                    yield DocumentChunk(
                        ordinal,
                        f"工作表 {sheet_name} · 行 {first_row}-{last_row}",
                        _xlsx_chunk_content(sheet_name, buffer),
                    )
                    ordinal += 1
                    buffer = []
                if on_progress and row_no % XLSX_PROGRESS_ROW_INTERVAL == 0:
                    on_progress(progress_label, row_no)

            if buffer:
                yield DocumentChunk(
                    ordinal,
                    f"工作表 {sheet_name} · 行 {first_row}-{last_row}",
                    _xlsx_chunk_content(sheet_name, buffer),
                )
                ordinal += 1

            if (
                on_progress
                and processed_row > 0
                and processed_row % XLSX_PROGRESS_ROW_INTERVAL != 0
            ):
                on_progress(progress_label, processed_row)
    finally:
        workbook.close()


@dataclass(slots=True, frozen=True)
class XlsxCalamineFastAdapter:
    """Rust-backed XLSX fast path with an all-or-nothing openpyxl fallback.

    The Calamine attempt is captured into the same bounded temporary spool used
    elsewhere by the indexing pipeline. Nothing is yielded until the workbook
    has been parsed successfully, so a mid-workbook parser failure can fall back
    to the mature openpyxl implementation without duplicate partial chunks.
    """

    name: str = "calamine-xlsx-fast"
    priority: int = 110
    extensions: frozenset[str] = frozenset({".xlsx"})

    def is_available(self) -> bool:
        return importlib.util.find_spec("python_calamine") is not None

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        if not self.is_available():
            yield from _iter_direct_document_chunks(
                path,
                target_chars=target_chars,
                spreadsheet_rows_per_chunk=spreadsheet_rows_per_chunk,
                on_progress=on_progress,
            )
            return

        spool = DocumentChunkSpool()
        try:
            try:
                spool.capture(
                    _iter_calamine_spreadsheet_chunks(
                        path,
                        rows_per_chunk=max(1, spreadsheet_rows_per_chunk),
                        on_progress=on_progress,
                    )
                )
            except Exception as exc:
                # Cancellation is control flow from DirectoryIndexer, not a
                # parser compatibility failure. Importing that type here would
                # create an indexer<->adapter cycle, so preserve the contract by
                # its stable exception name and let it propagate immediately.
                if type(exc).__name__ == "IndexCancelled":
                    raise
                # Discard every partial Calamine chunk before using the mature
                # compatibility path. The caller sees exactly one extraction.
                spool.close()
                yield from _iter_direct_document_chunks(
                    path,
                    target_chars=target_chars,
                    spreadsheet_rows_per_chunk=spreadsheet_rows_per_chunk,
                    on_progress=on_progress,
                )
                return

            yield from spool.iter_chunks()
        finally:
            try:
                spool.close()
            except Exception:
                pass


@dataclass(slots=True, frozen=True)
class DirectDocumentAdapter:
    """Existing mature parsers for OOXML/PDF/plain text."""

    name: str = "direct"
    priority: int = 100
    extensions: frozenset[str] = DIRECT_SUPPORTED_EXTENSIONS

    def is_available(self) -> bool:
        return True

    def supports_path(self, path: Path) -> bool:
        # Some WPS/Office files carry a .pptx suffix while retaining the old
        # OLE/CFB container. python-pptx cannot read that container, so let the
        # isolated Tika/WPS compatibility cascade inspect it instead.
        return not (
            path.suffix.lower() == ".pptx" and _has_ole_compound_header(path)
        )

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        yield from _iter_direct_document_chunks(
            path,
            target_chars=target_chars,
            spreadsheet_rows_per_chunk=spreadsheet_rows_per_chunk,
            on_progress=on_progress,
        )


@dataclass(slots=True, frozen=True)
class CalamineSpreadsheetAdapter:
    """Rust-backed spreadsheet reader exposed through python-calamine."""

    name: str = "calamine"
    priority: int = 80
    extensions: frozenset[str] = CALAMINE_CANDIDATE_EXTENSIONS
    rows_per_chunk: int = 200

    def is_available(self) -> bool:
        return importlib.util.find_spec("python_calamine") is not None

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        del target_chars
        if not self.is_available():
            raise AdapterUnavailable(
                "读取该表格格式需要可选组件 python-calamine；"
                "安装 DocSeek 的 calamine 可选依赖后可直接本地解析"
            )

        # Calamine loads legacy BIFF .xls workbooks eagerly before sheet rows
        # become iterable. Surface that otherwise silent phase so a large or
        # unusual workbook does not look like a frozen desktop application.
        if on_progress and path.suffix.lower() == ".xls":
            on_progress("正在打开旧版 Excel（.xls 工作簿会先整体加载）", 0)

        rows_per_chunk = spreadsheet_rows_per_chunk or self.rows_per_chunk
        yield from _iter_calamine_spreadsheet_chunks(
            path,
            rows_per_chunk=max(1, rows_per_chunk),
            on_progress=on_progress,
        )


@dataclass(slots=True, frozen=True)
class TikaNativeAdapter:
    """Broad-format local extraction through Rust-native iscc-tika.

    This backend is intentionally a compatibility layer. It does not fabricate
    page/slide/table coordinates when Tika only provides flat text; DocIR marks
    those chunks as generic so structure-aware ranking can distinguish fidelity.
    """

    name: str = "tika-native"
    priority: int = 70
    extensions: frozenset[str] = TIKA_NATIVE_CANDIDATE_EXTENSIONS

    def is_available(self) -> bool:
        return importlib.util.find_spec("iscc_tika") is not None

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        del spreadsheet_rows_per_chunk, on_progress
        if not self.is_available():
            raise AdapterUnavailable(
                "读取该旧版/开放文档格式需要可选组件 iscc-tika；"
                "该组件使用本地 Rust/Tika 原生库，不需要 Java 服务"
            )

        from iscc_tika import Extractor

        extractor = Extractor()
        text, _metadata = extractor.extract_file_to_string(str(path))
        if not isinstance(text, str):
            text = str(text)
        yield from _iter_flat_text_chunks(text, target_chars=max(1, target_chars))


@dataclass(slots=True, frozen=True)
class WpsNativeAdapter:
    """Thin vendor fallback to the installed WPS client."""

    name: str = "wps-local"
    priority: int = 60
    extensions: frozenset[str] = WPS_LOCAL_CANDIDATE_EXTENSIONS

    def is_available(self) -> bool:
        return any(can_convert_extension(extension) for extension in self.extensions)

    def supports_path(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions and can_convert_extension(path.suffix)

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        if not self.supports_path(path):
            raise AdapterUnavailable(
                f"{path.suffix.lower()} 需要本机 WPS Office 自动化组件和 DocSeek wps 可选依赖"
            )
        with converted_openxml(path) as converted:
            yield from _iter_direct_document_chunks(
                converted,
                target_chars=target_chars,
                spreadsheet_rows_per_chunk=spreadsheet_rows_per_chunk,
                on_progress=on_progress,
            )


class DocumentAdapterRegistry:
    def __init__(self, adapters: Iterable[DocumentAdapter] | None = None) -> None:
        registered = list(
            adapters
            or (
                XlsxCalamineFastAdapter(),
                DirectDocumentAdapter(),
                CalamineSpreadsheetAdapter(),
                TikaNativeAdapter(),
                WpsNativeAdapter(),
            )
        )
        self._adapters = tuple(
            sorted(registered, key=lambda adapter: adapter.priority, reverse=True)
        )

    @property
    def known_extensions(self) -> frozenset[str]:
        return frozenset(FORMAT_CAPABILITIES)

    def is_known_path(self, path: Path) -> bool:
        return path.suffix.lower() in self.known_extensions

    def candidates_for(self, path: Path) -> tuple[DocumentAdapter, ...]:
        extension = path.suffix.lower()
        return tuple(adapter for adapter in self._adapters if extension in adapter.extensions)

    def adapter_for(self, path: Path) -> DocumentAdapter:
        candidates = self.candidates_for(path)
        if not candidates:
            raise AdapterUnavailable(f"DocSeek 尚未注册该文件格式：{path.suffix.lower()}")

        unavailable: list[str] = []
        for adapter in candidates:
            supports_path = getattr(adapter, "supports_path", None)
            if supports_path is not None:
                if supports_path(path):
                    return adapter
                unavailable.append(adapter.name)
                continue
            if adapter.is_available():
                return adapter
            unavailable.append(adapter.name)

        names = " / ".join(unavailable)
        raise AdapterUnavailable(
            f"已识别 {path.suffix.lower()}，但当前机器没有可用的本地解析适配器（{names}）"
        )

    def can_handle(self, path: Path) -> bool:
        try:
            self.adapter_for(path)
            return True
        except AdapterUnavailable:
            return False

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        adapter = self.adapter_for(path)
        yield from adapter.iter_chunks(
            path,
            target_chars=target_chars,
            spreadsheet_rows_per_chunk=spreadsheet_rows_per_chunk,
            on_progress=on_progress,
        )


DEFAULT_ADAPTER_REGISTRY = DocumentAdapterRegistry()
