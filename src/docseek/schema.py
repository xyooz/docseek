from __future__ import annotations

import sqlite3


CURRENT_SCHEMA_VERSION = 9
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
    """Create/upgrade the lightweight per-file extraction-state sidecar.

    v8 introduced only ``revision``. v9 adds a durable status and timestamp
    without rewriting ``files``/``chunks`` or rebuilding FTS. Existing v8 rows
    intentionally default to INDEXED. The indexer still requires an actual
    chunk for INDEXED, so historical zero-chunk rows are repaired once and then
    recorded as NO_TEXT/OCR_REQUIRED instead of being parsed forever.
    """
    previous_version = get_schema_version(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS extraction_state(
            path TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'INDEXED',
            updated_at REAL NOT NULL DEFAULT 0
        )
        """
    )

    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(extraction_state)").fetchall()
    }
    if "status" not in columns:
        conn.execute(
            "ALTER TABLE extraction_state "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'INDEXED'"
        )
    if "updated_at" not in columns:
        conn.execute(
            "ALTER TABLE extraction_state "
            "ADD COLUMN updated_at REAL NOT NULL DEFAULT 0"
        )

    if previous_version < 8:
        conn.execute(
            """
            INSERT OR IGNORE INTO extraction_state(path, revision, status, updated_at)
            SELECT path, ?, 'INDEXED', 0 FROM files
            """,
            (LEGACY_EXTRACTION_REVISION,),
        )


def mark_schema_current(conn: sqlite3.Connection) -> None:
    _ensure_extraction_state(conn)
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
