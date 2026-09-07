from __future__ import annotations

import sqlite3


CURRENT_SCHEMA_VERSION = 11
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

    v8 introduced ``revision``. v9 added durable lifecycle status and a
    timestamp. v10 adds only failure retry metadata so repeatedly broken files
    can be deferred without touching the files/chunks/FTS tables.

    Existing v8 rows intentionally default to INDEXED. The indexer still
    requires an actual chunk for INDEXED, so historical zero-chunk rows are
    repaired once and then recorded as NO_TEXT/OCR_REQUIRED instead of being
    parsed forever.
    """
    previous_version = get_schema_version(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS extraction_state(
            path TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'INDEXED',
            updated_at REAL NOT NULL DEFAULT 0,
            source_modified_time REAL,
            source_size INTEGER,
            failure_count INTEGER NOT NULL DEFAULT 0,
            retry_after REAL NOT NULL DEFAULT 0
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
    if "source_modified_time" not in columns:
        conn.execute("ALTER TABLE extraction_state ADD COLUMN source_modified_time REAL")
    if "source_size" not in columns:
        conn.execute("ALTER TABLE extraction_state ADD COLUMN source_size INTEGER")
    if "failure_count" not in columns:
        conn.execute(
            "ALTER TABLE extraction_state "
            "ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
        )
    if "retry_after" not in columns:
        conn.execute(
            "ALTER TABLE extraction_state "
            "ADD COLUMN retry_after REAL NOT NULL DEFAULT 0"
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
    # Additive sidecar: old chunks remain readable without a full index rewrite.
    conn.execute("""CREATE TABLE IF NOT EXISTS chunk_structure(
        chunk_id INTEGER PRIMARY KEY,
        locator_version INTEGER NOT NULL DEFAULT 1,
        kind TEXT NOT NULL,
        title TEXT,
        page INTEGER, slide INTEGER, sheet TEXT,
        row_start INTEGER, row_end INTEGER,
        line_start INTEGER, line_end INTEGER,
        block_start INTEGER, block_end INTEGER
    )""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS chunk_structure_cleanup
        AFTER DELETE ON chunks BEGIN
            DELETE FROM chunk_structure WHERE chunk_id = OLD.id;
        END""")
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
