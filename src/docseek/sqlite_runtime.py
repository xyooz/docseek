from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import CURRENT_SCHEMA_VERSION, get_schema_version


def current_schema_objects(db_path: Path) -> frozenset[str] | None:
    """Return SQLite object names only when the on-disk schema is current.

    The probe is deliberately read-only. Runtime workers use it to distinguish
    an already-initialized DocSeek database from a database that genuinely
    needs bootstrap/migration work, without taking SQLite's writer slot or
    mutating database-wide journal state.
    """

    path = Path(db_path)
    if not path.exists():
        return None

    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        if get_schema_version(conn) != CURRENT_SCHEMA_VERSION:
            return None
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return frozenset(str(row[0]) for row in rows)
    finally:
        conn.close()


def ensure_wal_mode(conn: sqlite3.Connection) -> None:
    """Enable WAL only when the database is not already in WAL mode.

    ``PRAGMA journal_mode=WAL`` is database-wide and can need stronger locking
    than ordinary WAL reads/writes. Reissuing it on every worker startup is
    unnecessary and has caused lock contention on real Windows desktops.
    """

    row = conn.execute("PRAGMA journal_mode").fetchone()
    current = str(row[0]).casefold() if row else ""
    if current == "wal":
        return

    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    selected = str(row[0]).casefold() if row else ""
    if selected != "wal":
        raise RuntimeError(
            "DocSeek 无法将索引数据库切换到 WAL 模式；"
            f"SQLite 返回 journal_mode={selected or 'unknown'}。"
        )
