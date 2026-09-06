from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import pymupdf
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from pptx import Presentation


@dataclass(slots=True, frozen=True)
class DocumentChunk:
    ordinal: int
    location: str
    content: str


TEXT_EXTENSIONS = {".txt", ".md", ".log", ".csv"}
TEXT_ENCODING_SAMPLE_BYTES = 65_536
TEXT_ENCODING_CANDIDATES = ("utf-8", "gb18030")
XLSX_PROGRESS_ROW_INTERVAL = 1_000
ChunkProgressCallback = Callable[[str, int], None]


def iter_document_chunks(
    path: Path,
    *,
    target_chars: int = 12_000,
    xlsx_rows_per_chunk: int = 200,
    on_progress: ChunkProgressCallback | None = None,
) -> Iterator[DocumentChunk]:
    """Public extraction entrypoint used by the production indexer.

    Every supported format now passes through the capability-aware broker and
    streaming DocIR layer before being converted back to the existing v7
    ``DocumentChunk`` contract. The compatibility conversion is lossless, so
    this architectural upgrade does not change FTS text or require a schema
    migration. Structural labels may become richer as extractors learn titles.
    """
    from .extraction_broker import DEFAULT_EXTRACTION_BROKER

    yield from DEFAULT_EXTRACTION_BROKER.iter_chunks(
        path,
        target_chars=target_chars,
        spreadsheet_rows_per_chunk=xlsx_rows_per_chunk,
        on_progress=on_progress,
    )


def _iter_direct_document_chunks(
    path: Path,
    *,
    target_chars: int = 12_000,
    spreadsheet_rows_per_chunk: int = 200,
    on_progress: ChunkProgressCallback | None = None,
) -> Iterator[DocumentChunk]:
    """Mature built-in parsers used only by ``DirectDocumentAdapter``.

    Keeping this function separate from the public broker entrypoint prevents
    adapter recursion while preserving the already-tested low-memory streaming
    implementations.
    """
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        yield from _iter_text_chunks(path, target_chars=target_chars)
    elif suffix == ".docx":
        yield from _iter_docx_chunks(path, target_chars=target_chars)
    elif suffix == ".xlsx":
        yield from _iter_xlsx_chunks(
            path,
            rows_per_chunk=spreadsheet_rows_per_chunk,
            on_progress=on_progress,
        )
    elif suffix == ".pptx":
        yield from _iter_pptx_chunks(path)
    elif suffix == ".pdf":
        yield from _iter_pdf_chunks(path)
    else:
        raise ValueError(f"Unsupported direct file type: {suffix}")


def _iter_text_chunks(path: Path, *, target_chars: int) -> Iterator[DocumentChunk]:
    ordinal = 0
    start_line = 1
    line_no = 0
    buffer: list[str] = []
    char_count = 0

    for line_no, line in enumerate(_iter_text_lines(path), start=1):
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


def _iter_text_lines(path: Path) -> Iterator[str]:
    """Decode plain text with one file open and one probe read for small files.

    Encoding detection needs a bounded prefix. Historically DocSeek opened the
    file once for that 64 KiB probe and then opened it again from byte zero for
    normal text iteration. Small office-side text files therefore paid two file
    opens and read their whole payload twice.

    Read one extra byte with the probe so a complete small file can reuse the
    successful strict decode itself. Large files keep the established streaming
    TextIOWrapper path and bounded memory usage; they merely rewind the same
    handle instead of opening a second one.
    """
    with path.open("rb") as raw:
        probe = raw.read(TEXT_ENCODING_SAMPLE_BYTES + 1)
        has_more = len(probe) > TEXT_ENCODING_SAMPLE_BYTES

        if not has_more:
            _encoding, decoded = _decode_text_bytes(probe)
            with io.StringIO(decoded, newline=None) as text:
                yield from text
            return

        sample = probe[:TEXT_ENCODING_SAMPLE_BYTES]
        encoding = _detect_text_encoding_sample(sample)
        raw.seek(0)
        with io.TextIOWrapper(
            raw,
            encoding=encoding,
            errors="ignore",
            newline=None,
        ) as text:
            yield from text


def _decode_text_bytes(data: bytes) -> tuple[str, str]:
    for encoding in TEXT_ENCODING_CANDIDATES:
        try:
            return encoding, data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    return "utf-8", data.decode("utf-8", errors="ignore")


def _detect_text_encoding_sample(sample: bytes) -> str:
    encoding, _decoded = _decode_text_bytes(sample)
    return encoding


def _clean_location_title(text: str, *, max_chars: int = 80) -> str:
    """Normalize a human-readable structural title for compact result labels."""
    normalized = " ".join(text.split()).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def _is_docx_heading_style(style_name: str) -> bool:
    style = style_name.strip()
    folded = style.casefold()
    return (
        folded.startswith("heading")
        or folded == "title"
        or style.startswith("标题")
    )


def _iter_docx_chunks(path: Path, *, target_chars: int) -> Iterator[DocumentChunk]:
    document = Document(path)
    ordinal = 0
    buffer: list[str] = []
    char_count = 0
    section_start = 1
    block_no = 0
    current_heading = ""
    buffer_heading = ""

    def maybe_emit(force: bool = False) -> DocumentChunk | None:
        nonlocal ordinal, buffer, char_count, section_start, buffer_heading
        if not buffer or (not force and char_count < target_chars):
            return None
        content = "\n".join(buffer).strip()
        if not content:
            buffer = []
            char_count = 0
            buffer_heading = ""
            return None
        location = f"文档块 {section_start}-{block_no}"
        if buffer_heading:
            location += f" · 标题 {buffer_heading}"
        chunk = DocumentChunk(ordinal, location, content)
        ordinal += 1
        buffer = []
        char_count = 0
        buffer_heading = ""
        section_start = block_no + 1
        return chunk

    for element in document.element.body.iterchildren():
        heading = False
        if element.tag.endswith("}p"):
            paragraph = Paragraph(element, document)
            texts = [paragraph.text.strip()]
            style_name = str(getattr(getattr(paragraph, "style", None), "name", "") or "")
            heading = _is_docx_heading_style(style_name)
        elif element.tag.endswith("}tbl"):
            texts = ("\t".join(cell.text for cell in row.cells).strip()
                     for row in Table(element, document).rows)
        else:
            continue
        for text in texts:
            if not text:
                continue
            if heading:
                chunk = maybe_emit(force=True)
                if chunk:
                    yield chunk
                current_heading = _clean_location_title(text)
            block_no += 1
            if not buffer:
                buffer_heading = current_heading
            buffer.append(text)
            char_count += len(text)
            chunk = maybe_emit()
            if chunk:
                yield chunk

    chunk = maybe_emit(force=True)
    if chunk:
        yield chunk


def _xlsx_row_text(row: tuple[object, ...]) -> str:
    """Render one spreadsheet row without shifting later columns to the left."""
    cells = ["" if value is None else str(value) for value in row]
    while cells and not cells[-1]:
        cells.pop()
    if not cells or not any(cells):
        return ""
    return "\t".join(cells)


def _xlsx_chunk_content(sheet_title: str, rows: list[str]) -> str:
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
        title_shape = getattr(slide.shapes, "title", None)
        title_text = _clean_location_title(str(getattr(title_shape, "text", "") or ""))
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text:
                content.append(str(text))
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    content.append("\t".join(cell.text for cell in row.cells))
        text = "\n".join(content).strip()
        if text:
            location = f"幻灯片 {slide_no}"
            if title_text:
                location += f" · 标题 {title_text}"
            yield DocumentChunk(ordinal, location, text)
            ordinal += 1


def _iter_pdf_chunks(path: Path) -> Iterator[DocumentChunk]:
    ordinal = 0
    with pymupdf.open(path) as document:
        for page_no, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if text:
                yield DocumentChunk(ordinal, f"第 {page_no} 页", text)
                ordinal += 1
