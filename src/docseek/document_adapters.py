from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from .chunks import (
    XLSX_PROGRESS_ROW_INTERVAL,
    ChunkProgressCallback,
    DocumentChunk,
    _xlsx_chunk_content,
    _xlsx_row_text,
    iter_document_chunks,
)
from .document_types import (
    CALAMINE_CANDIDATE_EXTENSIONS,
    DIRECT_SUPPORTED_EXTENSIONS,
    FORMAT_CAPABILITIES,
    WPS_LOCAL_CANDIDATE_EXTENSIONS,
)
from .wps_adapter import can_convert_extension, converted_openxml


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
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]: ...


@dataclass(slots=True, frozen=True)
class DirectDocumentAdapter:
    """Existing mature parsers for OOXML/PDF/plain text."""

    name: str = "direct"
    priority: int = 100
    extensions: frozenset[str] = DIRECT_SUPPORTED_EXTENSIONS

    def is_available(self) -> bool:
        return True

    def iter_chunks(
        self,
        path: Path,
        *,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        yield from iter_document_chunks(path, on_progress=on_progress)


@dataclass(slots=True, frozen=True)
class CalamineSpreadsheetAdapter:
    """Rust-backed spreadsheet reader exposed through python-calamine.

    XLSX deliberately remains on the existing openpyxl path for now. Calamine
    first adds formats that the direct parser cannot read. The registry can
    later change priorities for large XLSX files after a real workload A/B,
    without changing the indexing contract.
    """

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
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        if not self.is_available():
            raise AdapterUnavailable(
                "读取该表格格式需要可选组件 python-calamine；"
                "安装 DocSeek 的 calamine 可选依赖后可直接本地解析"
            )

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
                    row_text = _xlsx_row_text(tuple(row))
                    if row_text:
                        if not buffer:
                            first_row = row_no
                        buffer.append(row_text)
                        last_row = row_no
                    if buffer and len(buffer) >= self.rows_per_chunk:
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
class WpsNativeAdapter:
    """Thin local bridge to the installed WPS client.

    WPS/legacy files are converted to a temporary OOXML copy and then handed to
    the already-tested direct chunk parsers. The source file is never modified
    and no online conversion service is used.
    """

    name: str = "wps-local"
    priority: int = 60
    extensions: frozenset[str] = WPS_LOCAL_CANDIDATE_EXTENSIONS

    def is_available(self) -> bool:
        # Availability is format-family specific, so registry selection performs
        # the final check through supports_path().
        return any(can_convert_extension(extension) for extension in self.extensions)

    def supports_path(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions and can_convert_extension(path.suffix)

    def iter_chunks(
        self,
        path: Path,
        *,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        if not self.supports_path(path):
            raise AdapterUnavailable(
                f"{path.suffix.lower()} 需要本机 WPS Office 自动化组件和 DocSeek wps 可选依赖"
            )
        with converted_openxml(path) as converted:
            yield from iter_document_chunks(converted, on_progress=on_progress)


class DocumentAdapterRegistry:
    def __init__(self, adapters: Iterable[DocumentAdapter] | None = None) -> None:
        registered = list(
            adapters
            or (
                DirectDocumentAdapter(),
                CalamineSpreadsheetAdapter(),
                WpsNativeAdapter(),
            )
        )
        self._adapters = tuple(sorted(registered, key=lambda adapter: adapter.priority, reverse=True))

    @property
    def known_extensions(self) -> frozenset[str]:
        # Include every registered product capability, even when its optional
        # parser is missing. This lets the indexer persist a useful issue instead
        # of silently pretending a known WPS/legacy file does not exist.
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
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        adapter = self.adapter_for(path)
        yield from adapter.iter_chunks(path, on_progress=on_progress)


DEFAULT_ADAPTER_REGISTRY = DocumentAdapterRegistry()
