from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from .document_types import DIRECT_SUPPORTED_EXTENSIONS


def is_fast_lane_path(path: Path) -> bool:
    """Return True for mature in-process formats safe to index immediately."""
    return Path(path).suffix.lower() in DIRECT_SUPPORTED_EXTENSIONS


def prioritize_index_candidates(candidates: Iterable[Path]) -> Iterator[Path]:
    """Stream fast-lane files immediately and defer compatibility formats.

    Discovery remains lazy: modern DOCX/XLSX/PPTX/PDF/text documents become
    searchable while the filesystem walk continues. Legacy/compatibility files
    are retained in stable discovery order and replayed only after the fast lane
    is exhausted, so one slow XLS/DOC/PPT cannot block later modern documents.
    """
    compatibility_lane: list[Path] = []
    for path in candidates:
        candidate = Path(path)
        if is_fast_lane_path(candidate):
            yield candidate
        else:
            compatibility_lane.append(candidate)

    yield from compatibility_lane
