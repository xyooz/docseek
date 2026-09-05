from __future__ import annotations

import sqlite3


CURRENT_SCHEMA_VERSION = 8
LEGACY_EXTRACTION_REVISION = 1


class UnsupportedSchemaVersion(RuntimeError):
    """Raised when a database was created by a newer DocSeek version."""


def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def ensure_schema_compatible(conn: sqlite3.Connection) -> int:
    """Reject databases newer than this build before mutating their schema."""
    version = get_schema_version(conn)
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            "索引数据库版本高于当前 DocSeek 支持范围："
            f"数据库 v{version}，当前程序 v{CURRENT_SCHEMA_VERSION}。"
            "请使用更新版本的 DocSeek 打开该索引。"
        )
    return version


def _ensure_extraction_state(conn: sqlite3.Connection) -> None:
    """Create the lightweight per-file extraction revision sidecar.

    v8 deliberately keeps this metadata outside ``files`` and ``chunks`` so the
    migration does not rewrite either hot table or rebuild FTS. Existing indexes
    are seeded at revision 1. Format-specific extractors can then opt into newer
    revisions and be refreshed lazily by the normal reconciliation scan.
    """
    previous_version = get_schema_version(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS extraction_state(
            path TEXT PRIMARY KEY,
            revision INTEGER NOT NULL
        )
        """
    )
    if previous_version < 8:
        conn.execute(
            """
            INSERT OR IGNORE INTO extraction_state(path, revision)
            SELECT path, ? FROM files
            """,
            (LEGACY_EXTRACTION_REVISION,),
        )


def mark_schema_current(conn: sqlite3.Connection) -> None:
    _ensure_extraction_state(conn)
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
