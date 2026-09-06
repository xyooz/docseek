from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from .document_types import DIRECT_SUPPORTED_EXTENSIONS


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
    fast_lane: list[Path] = []
    compatibility_lane: list[Path] = []

    for path in candidates:
        candidate = Path(path)
        if is_fast_lane_path(candidate):
            fast_lane.append(candidate)
        else:
            compatibility_lane.append(candidate)

    yield from fast_lane
    yield from compatibility_lane
