from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import pymupdf
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation


@dataclass(slots=True, frozen=True)
class DocumentChunk:
    ordinal: int
    location: str
    content: str


TEXT_EXTENSIONS = {".txt", ".md", ".log", ".csv"}
XLSX_PROGRESS_ROW_INTERVAL = 1_000
ChunkProgressCallback = Callable[[str, int], None]


def iter_document_chunks(
    path: Path,
    *,
    target_chars: int = 12_000,
    xlsx_rows_per_chunk: int = 200,
    on_progress: ChunkProgressCallback | None = None,
) -> Iterator[DocumentChunk]:
    """Yield bounded, location-aware chunks without building one giant body string.

    XLSX callers may provide ``on_progress`` to receive lightweight streaming
    row progress. The callback is invoked at a bounded cadence while the same
    read-only worksheet iterator is already being consumed, so progress
    reporting does not require a second workbook pass or retain worksheet rows.
    """
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        yield from _iter_text_chunks(path, target_chars=target_chars)
    elif suffix == ".docx":
        yield from _iter_docx_chunks(path, target_chars=target_chars)
    elif suffix == ".xlsx":
        yield from _iter_xlsx_chunks(
            path,
            rows_per_chunk=xlsx_rows_per_chunk,
            on_progress=on_progress,
        )
    elif suffix == ".pptx":
        yield from _iter_pptx_chunks(path)
    elif suffix == ".pdf":
        yield from _iter_pdf_chunks(path)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


def _iter_text_chunks(path: Path, *, target_chars: int) -> Iterator[DocumentChunk]:
    encoding = _detect_text_encoding(path)
    ordinal = 0
    start_line = 1
    line_no = 0
    buffer: list[str] = []
    char_count = 0

    with path.open("r", encoding=encoding, errors="ignore") as handle:
        for line_no, line in enumerate(handle, start=1):
            buffer.append(line.rstrip("\n"))
            char_count += len(line)
            if char_count >= target_chars:
                content = "\n".join(buffer).strip()
                if content:
                    yield DocumentChunk(ordinal, f"行 {start_line}-{line_no}", content)
                    ordinal += 1
                buffer = []
                char_count = 0
                start_line = line_no + 1

    content = "\n".join(buffer).strip()
    if content:
        end_line = max(start_line, line_no)
        yield DocumentChunk(ordinal, f"行 {start_line}-{end_line}", content)


def _detect_text_encoding(path: Path) -> str:
    sample = path.read_bytes()[:65536]
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            sample.decode(encoding, errors="strict")
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _iter_docx_chunks(path: Path, *, target_chars: int) -> Iterator[DocumentChunk]:
    document = Document(path)
    ordinal = 0
    buffer: list[str] = []
    char_count = 0
    section_start = 1
    block_no = 0

    def maybe_emit(force: bool = False) -> DocumentChunk | None:
        nonlocal ordinal, buffer, char_count, section_start
        if not buffer or (not force and char_count < target_chars):
            return None
        content = "\n".join(buffer).strip()
        if not content:
            buffer = []
            char_count = 0
            return None
        chunk = DocumentChunk(ordinal, f"文档块 {section_start}-{block_no}", content)
        ordinal += 1
        buffer = []
        char_count = 0
        section_start = block_no + 1
        return chunk

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        block_no += 1
        buffer.append(text)
        char_count += len(text)
        chunk = maybe_emit()
        if chunk:
            yield chunk

    for table in document.tables:
        for row in table.rows:
            text = "\t".join(cell.text for cell in row.cells).strip()
            if not text:
                continue
            block_no += 1
            buffer.append(text)
            char_count += len(text)
            chunk = maybe_emit()
            if chunk:
                yield chunk

    chunk = maybe_emit(force=True)
    if chunk:
        yield chunk


def _xlsx_row_text(row: tuple[object, ...]) -> str:
    """Render one Excel row without shifting later columns to the left.

    Empty cells between populated cells are represented by empty tab fields.
    Trailing empty cells are removed so a wide formatted worksheet does not
    generate enormous runs of meaningless separators.
    """
    cells = ["" if value is None else str(value) for value in row]
    while cells and not cells[-1]:
        cells.pop()
    if not cells or not any(cells):
        return ""
    return "\t".join(cells)


def _xlsx_chunk_content(sheet_title: str, rows: list[str]) -> str:
    # Sheet names are useful business context (e.g. “客户明细”/“逾期清单”) and
    # should be searchable even when they do not appear inside worksheet cells.
    return f"工作表: {sheet_title}\n" + "\n".join(rows)


def _iter_xlsx_chunks(
    path: Path,
    *,
    rows_per_chunk: int,
    on_progress: ChunkProgressCallback | None = None,
) -> Iterator[DocumentChunk]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    ordinal = 0
    try:
        for worksheet in workbook.worksheets:
            buffer: list[str] = []
            first_row = 1
            last_row = 0
            processed_row = 0
            progress_label = f"工作表 {worksheet.title}"
            for row_no, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
                processed_row = row_no
                row_text = _xlsx_row_text(row)
                if row_text:
                    if not buffer:
                        first_row = row_no
                    buffer.append(row_text)
                    last_row = row_no
                if buffer and len(buffer) >= rows_per_chunk:
                    yield DocumentChunk(
                        ordinal,
                        f"工作表 {worksheet.title} · 行 {first_row}-{last_row}",
                        _xlsx_chunk_content(worksheet.title, buffer),
                    )
                    ordinal += 1
                    buffer = []
                if on_progress and row_no % XLSX_PROGRESS_ROW_INTERVAL == 0:
                    on_progress(progress_label, row_no)
            if buffer:
                yield DocumentChunk(
                    ordinal,
                    f"工作表 {worksheet.title} · 行 {first_row}-{last_row}",
                    _xlsx_chunk_content(worksheet.title, buffer),
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


def _iter_pptx_chunks(path: Path) -> Iterator[DocumentChunk]:
    presentation = Presentation(path)
    ordinal = 0
    for slide_no, slide in enumerate(presentation.slides, start=1):
        content: list[str] = []
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text:
                content.append(str(text))
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    content.append("\t".join(cell.text for cell in row.cells))
        text = "\n".join(content).strip()
        if text:
            yield DocumentChunk(ordinal, f"幻灯片 {slide_no}", text)
            ordinal += 1


def _iter_pdf_chunks(path: Path) -> Iterator[DocumentChunk]:
    ordinal = 0
    with pymupdf.open(path) as document:
        for page_no, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if text:
                yield DocumentChunk(ordinal, f"第 {page_no} 页", text)
                ordinal += 1
