"""Persist DocIR locators independently from presentation labels."""
from pathlib import Path

from .doc_ir import block_from_chunk
from .document_types import document_family_for_extension

FIELDS = ("kind", "title", "page", "slide", "sheet", "row_start", "row_end",
          "line_start", "line_end", "block_start", "block_end")


def structure_for_chunk(path, extension, chunk):
    family = document_family_for_extension(extension)
    if family is None:
        return {"kind": "generic"}
    block = block_from_chunk(Path(path), family, chunk)
    return {"kind": str(block.kind), "title": block.title,
            **{name: getattr(block.locator, name) for name in FIELDS[2:]}}


def write_structure(conn, chunk_id, path, extension, chunk):
    values = structure_for_chunk(path, extension, chunk)
    conn.execute(
        "INSERT INTO chunk_structure(chunk_id, " + ",".join(FIELDS) + ") VALUES ("
        + ",".join("?" for _ in range(len(FIELDS) + 1)) + ")",
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
