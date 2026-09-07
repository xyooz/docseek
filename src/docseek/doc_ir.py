from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from .chunks import DocumentChunk
from .document_types import DocumentFamily


class BlockKind(StrEnum):
    """Format-neutral structural units used by the retrieval layer."""

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

    ``text`` is exactly what the existing FTS pipeline receives. Structure is
    additive metadata; adapters that cannot prove a locator are represented as
    ``GENERIC`` rather than inventing page/slide/table coordinates.
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
_SLIDE_LOCATION = re.compile(r"^幻灯片\s*(\d+)(?:\s*·\s*标题\s+(.+))?$")
_SHEET_LOCATION = re.compile(r"^工作表\s+(.+?)\s*·\s*行\s+(\d+)-(\d+)$")
_LINE_LOCATION = re.compile(r"^行\s+(\d+)-(\d+)$")
_WRITER_LOCATION = re.compile(r"^文档块\s+(\d+)-(\d+)(?:\s*·\s*标题\s+(.+))?$")


def _generic_block(
    family: DocumentFamily,
    chunk: DocumentChunk,
) -> DocumentBlock:
    return DocumentBlock(
        chunk.ordinal,
        BlockKind.GENERIC,
        chunk.content,
        BlockLocator(chunk.location),
        family,
    )


def block_from_chunk(
    path: Path,
    family: DocumentFamily,
    chunk: DocumentChunk,
) -> DocumentBlock:
    """Lift the current location-aware chunk stream into DocIR losslessly."""
    del path  # reserved for future provenance/enricher metadata.
    location = chunk.location

    if family == DocumentFamily.PDF:
        match = _PDF_LOCATION.match(location)
        if not match:
            return _generic_block(family, chunk)
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.PAGE,
            chunk.content,
            BlockLocator(location, page=int(match.group(1))),
            family,
        )

    if family == DocumentFamily.PRESENTATION:
        match = _SLIDE_LOCATION.match(location)
        if not match:
            return _generic_block(family, chunk)
        slide_no, title = match.groups()
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.SLIDE,
            chunk.content,
            BlockLocator(location, slide=int(slide_no)),
            family,
            title=title,
        )

    if family == DocumentFamily.SPREADSHEET:
        match = _SHEET_LOCATION.match(location)
        if not match:
            return _generic_block(family, chunk)
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

    if family == DocumentFamily.TEXT:
        match = _LINE_LOCATION.match(location)
        if not match:
            return _generic_block(family, chunk)
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.TEXT_RANGE,
            chunk.content,
            BlockLocator(
                location,
                line_start=int(match.group(1)),
                line_end=int(match.group(2)),
            ),
            family,
        )

    if family == DocumentFamily.WRITER:
        match = _WRITER_LOCATION.match(location)
        if not match:
            return _generic_block(family, chunk)
        start, end, title = match.groups()
        return DocumentBlock(
            chunk.ordinal,
            BlockKind.WRITER_BLOCK,
            chunk.content,
            BlockLocator(
                location,
                block_start=int(start),
                block_end=int(end),
            ),
            family,
            title=title,
        )

    return _generic_block(family, chunk)
