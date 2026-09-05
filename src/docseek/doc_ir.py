from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from .chunks import DocumentChunk
from .document_types import DocumentFamily


class BlockKind(StrEnum):
    """Format-neutral structural units used by the retrieval layer.

    The first version deliberately models only structure that DocSeek can
    already identify reliably. Adapters may enrich blocks later (headings,
    table headers, key/value pairs, visual regions) without changing the
    storage/search contract all at once.
    """

    TEXT_RANGE = "text-range"
    WRITER_BLOCK = "writer-block"
    SHEET_ROWS = "sheet-rows"
    SLIDE = "slide"
    PAGE = "page"
    GENERIC = "generic"


@dataclass(slots=True, frozen=True)
class BlockLocator:
    label: str
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    block_start: int | None = None
    block_end: int | None = None


@dataclass(slots=True, frozen=True)
class DocumentBlock:
    """One streaming DocIR block.

    ``text`` remains the exact text handed to the existing FTS pipeline. The
    structured fields are additive metadata for future structure-aware ranking
    and previews. This means introducing DocIR does not silently change current
    search semantics or require a schema migration.
    """

    ordinal: int
    kind: BlockKind
    text: str
    locator: BlockLocator
    family: DocumentFamily
    title: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()

    def as_chunk(self) -> DocumentChunk:
        return DocumentChunk(self.ordinal, self.locator.label, self.text)

    def attribute_map(self) -> Mapping[str, str]:
        return dict(self.attributes)


_PDF_LOCATION = re.compile(r"^第\s*(\d+)\s*页$")
_SLIDE_LOCATION = re.compile(r"^幻灯片\s*(\d+)$")
_SHEET_LOCATION = re.compile(r"^工作表\s+(.+?)\s*·\s*行\s+(\d+)-(\d+)$")
_LINE_LOCATION = re.compile(r"^行\s+(\d+)-(\d+)$")
_WRITER_LOCATION = re.compile(r"^文档块\s+(\d+)-(\d+)$")


def block_from_chunk(
    path: Path,
    family: DocumentFamily,
    chunk: DocumentChunk,
) -> DocumentBlock:
    """Lift the current location-aware chunk stream into DocIR.

    This compatibility bridge is intentionally lossless: the generated chunk
    can be converted back with ``as_chunk()`` byte-for-byte at the text level.
    New adapters may produce richer DocumentBlock objects directly later.
    """

    location = chunk.location

    if family == DocumentFamily.PDF:
        match = _PDF_LOCATION.match(location)
        page = int(match.group(1)) if match else None
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.PAGE,
            chunk.content,
            BlockLocator(location, page=page),
            family,
        )

    if family == DocumentFamily.PRESENTATION:
        match = _SLIDE_LOCATION.match(location)
        slide = int(match.group(1)) if match else None
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.SLIDE,
            chunk.content,
            BlockLocator(location, slide=slide),
            family,
        )

    if family == DocumentFamily.SPREADSHEET:
        match = _SHEET_LOCATION.match(location)
        if match:
            sheet, start, end = match.groups()
            return DocumentBlock(
                chunk.ordinal,
                BlockKind.SHEET_ROWS,
                chunk.content,
                BlockLocator(
                    location,
                    sheet=sheet,
                    row_start=int(start),
                    row_end=int(end),
                ),
                family,
                title=sheet,
            )
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.SHEET_ROWS,
            chunk.content,
            BlockLocator(location),
            family,
        )

    if family == DocumentFamily.TEXT:
        match = _LINE_LOCATION.match(location)
        start = int(match.group(1)) if match else None
        end = int(match.group(2)) if match else None
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.TEXT_RANGE,
            chunk.content,
            BlockLocator(location, line_start=start, line_end=end),
            family,
        )

    if family == DocumentFamily.WRITER:
        match = _WRITER_LOCATION.match(location)
        start = int(match.group(1)) if match else None
        end = int(match.group(2)) if match else None
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.WRITER_BLOCK,
            chunk.content,
            BlockLocator(location, block_start=start, block_end=end),
            family,
        )

    return DocumentBlock(
        chunk.ordinal,
        BlockKind.GENERIC,
        chunk.content,
        BlockLocator(location),
        family,
    )
