from __future__ import annotations

import json
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .file_exclusions import FileExclusionStore
from .index_health import capture_index_health
from .index_issues import IndexIssueStore
from .schema import CURRENT_SCHEMA_VERSION
from .search_db import SearchDatabase


DIAGNOSTIC_REPORT_VERSION = 1


def _utc_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return None


def build_diagnostic_report(
    database: SearchDatabase,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a support report that deliberately excludes user document data.

    The report contains aggregate health/runtime information only. It must not
    contain index-root paths, excluded paths, issue paths/details, filenames,
    document text, search history or saved queries.
    """

    snapshot = capture_index_health(database)
    issue_summary = IndexIssueStore(database.db_path).summary()
    file_patterns = FileExclusionStore(database).patterns()

    with database.connect() as conn:
        schema_row = conn.execute("PRAGMA user_version").fetchone()
        journal_row = conn.execute("PRAGMA journal_mode").fetchone()
    actual_schema_version = int(schema_row[0]) if schema_row else 0
    journal_mode = str(journal_row[0]) if journal_row else "unknown"

    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    return {
        "report_version": DIAGNOSTIC_REPORT_VERSION,
        "generated_at_utc": now.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "docseek": {
            "version": __version__,
            "frozen": bool(getattr(sys, "frozen", False)),
            "schema_expected": CURRENT_SCHEMA_VERSION,
            "schema_actual": actual_schema_version,
        },
        "runtime": {
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "sqlite": sqlite3.sqlite_version,
        },
        "index": {
            "indexed_files": snapshot.indexed_files,
            "storage_bytes": snapshot.storage_bytes,
            "roots": {
                "total": snapshot.total_roots,
                "active": snapshot.active_roots,
                "paused": snapshot.paused_roots,
            },
            "issues": {
                "total": snapshot.issue_count,
                "by_error_code": dict(sorted(issue_summary.items())),
            },
            "last_successful_reconcile_at_utc": _utc_iso(
                snapshot.last_successful_reconcile_at
            ),
            "max_file_size_mb": database.get_max_file_size_mb(),
            "excluded_directory_count": len(database.get_excluded_paths()),
            "excluded_file_pattern_count": len(file_patterns),
            "journal_mode": journal_mode,
        },
        "privacy": {
            "contains_document_text": False,
            "contains_filenames": False,
            "contains_file_paths": False,
            "contains_index_root_paths": False,
            "contains_issue_details": False,
            "contains_search_history": False,
        },
    }


def diagnostic_json(database: SearchDatabase) -> str:
    return json.dumps(
        build_diagnostic_report(database),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def write_diagnostic_report(database: SearchDatabase, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(diagnostic_json(database), encoding="utf-8")
    return destination
