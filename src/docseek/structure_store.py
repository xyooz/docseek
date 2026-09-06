"""Persist DocIR locators independently from presentation labels."""
from pathlib import Path

from .doc_ir import BlockKind, block_from_chunk
from .document_types import document_family_for_extension

FIELDS = ("kind", "title", "page", "slide", "sheet", "row_start", "row_end",
          "line_start", "line_end", "block_start", "block_end")
_DIRECT_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".log", ".csv", ".tsv"})
_INSERT_SQL = (
    "INSERT INTO chunk_structure(chunk_id, " + ",".join(FIELDS) + ") VALUES ("
    + ",".join("?" for _ in range(len(FIELDS) + 1)) + ")"
)


def _direct_text_line_range(location: str) -> tuple[int, int] | None:
    """Parse the exact line-range label emitted by the direct text extractor.

    Compatibility/Tika text formats use generic ``内容块`` labels and therefore
    deliberately fall through to the normal DocIR lifting path.
    """
    if not location.startswith("行 "):
        return None
    start_text, separator, end_text = location[2:].partition("-")
    if not separator:
        return None
    try:
        return int(start_text), int(end_text)
    except ValueError:
        return None


def structure_for_chunk(path, extension, chunk):
    normalized_extension = str(extension).lower()
    if normalized_extension in _DIRECT_TEXT_EXTENSIONS:
        line_range = _direct_text_line_range(chunk.location)
        if line_range is not None:
            line_start, line_end = line_range
            return {
                "kind": str(BlockKind.TEXT_RANGE),
                "title": None,
                "line_start": line_start,
                "line_end": line_end,
            }

    family = document_family_for_extension(extension)
    if family is None:
        return {"kind": "generic"}
    block = block_from_chunk(Path(path), family, chunk)
    return {"kind": str(block.kind), "title": block.title,
            **{name: getattr(block.locator, name) for name in FIELDS[2:]}}


def write_structure(conn, chunk_id, path, extension, chunk):
    normalized_extension = str(extension).lower()
    if normalized_extension in _DIRECT_TEXT_EXTENSIONS:
        line_range = _direct_text_line_range(chunk.location)
        if line_range is not None:
            line_start, line_end = line_range
            conn.execute(
                _INSERT_SQL,
                (
                    chunk_id,
                    str(BlockKind.TEXT_RANGE),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    line_start,
                    line_end,
                    None,
                    None,
                ),
            )
            return

    values = structure_for_chunk(path, extension, chunk)
    conn.execute(
        _INSERT_SQL,
        (chunk_id, *(values.get(name) for name in FIELDS)),
    )


def preview_location(result):
    """Use typed locations for new rows; old indexes retain their labels."""
    data = getattr(result, "structure", None) or {}
    if result.extension.lower() == ".pdf" and data.get("page"):
        return f"第 {data['page']} 页"
    if result.extension.lower() == ".xlsx" and data.get("sheet") and data.get("row_start"):
        return f"工作表 {data['sheet']} · 行 {data['row_start']}-{data['row_end']}"
    return result.location
