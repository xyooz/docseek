from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from .document_types import DIRECT_SUPPORTED_EXTENSIONS


MODERN_OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx", ".pdf"})
OFFICE_COMPATIBILITY_EXTENSIONS = frozenset(
    {
        ".doc", ".dot", ".rtf", ".odt", ".ppt", ".pps", ".odp",
        ".xls", ".xlsb", ".ods", ".wps", ".wpt", ".et", ".ett",
        ".xlt", ".dps", ".dpt",
    }
)


def is_fast_lane_path(path: Path) -> bool:
    """Return True for mature in-process formats that should be indexed first."""
    return Path(path).suffix.lower() in DIRECT_SUPPORTED_EXTENSIONS


def prioritize_index_candidates(candidates: Iterable[Path]) -> Iterator[Path]:
    """Finish filesystem discovery before replaying prioritized index lanes.

    Directory discovery is intentionally completed before the first candidate is
    yielded. The directory walker also reconciles persistent issue metadata; if
    indexing starts a batched SQLite write transaction while that lazy walker is
    still running, entering the next subdirectory can otherwise attempt a second
    metadata write from the same process and self-contend on SQLite's single WAL
    writer slot.

    Discovery is cheap compared with Office/PDF extraction and keeps only paths
    in memory. Once the snapshot is complete, mature/direct formats are replayed
    first and compatibility formats retain stable encounter order. This trades a
    small first-index discovery delay for a much stronger writer-ownership
    invariant on real multi-level Windows directory trees.
    """
    modern_office: list[Path] = []
    lightweight: list[Path] = []
    office_compatibility: list[Path] = []
    other_compatibility: list[Path] = []

    for path in candidates:
        candidate = Path(path)
        extension = candidate.suffix.lower()
        if extension in MODERN_OFFICE_EXTENSIONS:
            modern_office.append(candidate)
        elif extension in DIRECT_SUPPORTED_EXTENSIONS:
            lightweight.append(candidate)
        elif extension in OFFICE_COMPATIBILITY_EXTENSIONS:
            office_compatibility.append(candidate)
        else:
            # Unknown candidates are retained for callers that use this helper
            # independently; the production discovery path only yields known
            # formats.
            other_compatibility.append(candidate)

    yield from modern_office
    yield from lightweight
    yield from office_compatibility
    yield from other_compatibility
