from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .index_issues import IndexIssueStore
from .index_root_state import IndexRootStateStore
from .search_db import SearchDatabase


LAST_SUCCESSFUL_RECONCILE_AT_KEY = "last_successful_reconcile_at_v1"


@dataclass(frozen=True, slots=True)
class IndexHealthSnapshot:
    indexed_files: int
    storage_bytes: int
    total_roots: int
    active_roots: int
    paused_roots: int
    issue_count: int
    last_successful_reconcile_at: float | None


def index_storage_bytes(db_path: Path) -> int:
    """Return the current SQLite footprint, including live WAL/SHM files."""

    total = 0
    for path in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    ):
        try:
            total += path.stat().st_size
        except (FileNotFoundError, OSError):
            continue
    return total


def record_successful_reconcile(
    database: SearchDatabase,
    *,
    completed_at: float | None = None,
) -> float:
    """Persist the completion time of a successful full-root reconciliation."""

    timestamp = time.time() if completed_at is None else float(completed_at)
    database._set_setting(LAST_SUCCESSFUL_RECONCILE_AT_KEY, repr(timestamp))
    return timestamp


def get_last_successful_reconcile(database: SearchDatabase) -> float | None:
    raw = database._get_setting(LAST_SUCCESSFUL_RECONCILE_AT_KEY)
    if not raw:
        return None
    try:
        timestamp = float(raw)
    except (TypeError, ValueError):
        return None
    return timestamp if timestamp >= 0 else None


def capture_index_health(database: SearchDatabase) -> IndexHealthSnapshot:
    root_state = IndexRootStateStore(database)
    roots = database.get_index_roots()
    paused = root_state.paused_roots()
    active_count = max(0, len(roots) - len(paused))
    return IndexHealthSnapshot(
        indexed_files=database.count_files(),
        storage_bytes=index_storage_bytes(database.db_path),
        total_roots=len(roots),
        active_roots=active_count,
        paused_roots=len(paused),
        issue_count=IndexIssueStore(database.db_path).count(),
        last_successful_reconcile_at=get_last_successful_reconcile(database),
    )


def format_storage_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_reconcile_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "尚未记录（完成一次完整刷新后显示）"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "时间记录不可用"


def format_index_health(snapshot: IndexHealthSnapshot) -> str:
    roots = (
        f"目录 {snapshot.total_roots}"
        f"（监测 {snapshot.active_roots} / 暂停 {snapshot.paused_roots}）"
    )
    return (
        f"{snapshot.indexed_files:,} 个文件 · "
        f"占用 {format_storage_size(snapshot.storage_bytes)} · "
        f"{roots} · 问题 {snapshot.issue_count}"
    )
