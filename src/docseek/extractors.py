from __future__ import annotations

from pathlib import Path

import fitz
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation


SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".log", ".csv",
    ".docx", ".xlsx", ".pptx", ".pdf",
}


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix in {".txt", ".md", ".log", ".csv"}:
        return _extract_plain_text(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".xlsx":
        return _extract_xlsx(path)
    if suffix == ".pptx":
        return _extract_pptx(path)
    if suffix == ".pdf":
        return _extract_pdf(path)

    raise ValueError(f"Unsupported file type: {suffix}")


def _extract_plain_text(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _extract_docx(path: Path) -> str:
    document = Document(path)
    chunks = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    for table in document.tables:
        for row in table.rows:
            chunks.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(chunks)


def _extract_xlsx(path: Path) -> str:
    workbook = load_workbook(path, read_only=True, data_only=True)
    chunks: list[str] = []
    try:
        for worksheet in workbook.worksheets:
            chunks.append(f"[Sheet: {worksheet.title}]")
            for row in worksheet.iter_rows(values_only=True):
                values = [str(value) for value in row if value is not None]
                if values:
                    chunks.append("\t".join(values))
    finally:
        workbook.close()
    return "\n".join(chunks)


def _extract_pptx(path: Path) -> str:
    presentation = Presentation(path)
    chunks: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        slide_chunks: list[str] = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                slide_chunks.append(shape.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    slide_chunks.append("\t".join(cell.text for cell in row.cells))
        if slide_chunks:
            chunks.append(f"[Slide: {index}]")
            chunks.extend(slide_chunks)
    return "\n".join(chunks)


def _extract_pdf(path: Path) -> str:
    chunks: list[str] = []
    with fitz.open(path) as document:
        for page_no, page in enumerate(document, start=1):
            text = page.get_text("text")
            if text:
                chunks.append(f"[Page: {page_no}]\n{text}")
    return "\n".join(chunks)
