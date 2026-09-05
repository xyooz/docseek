from __future__ import annotations

import os
from pathlib import Path

from .chunk_store import ChunkStore


def _path_key(path: str | Path) -> str:
    """Return a stable comparison key for Windows and POSIX paths."""
    candidate = Path(path)
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()
    return os.path.normcase(os.path.normpath(str(candidate)))


def _is_under_root(path_key: str, root_key: str) -> bool:
    try:
        common = os.path.commonpath((path_key, root_key))
    except ValueError:
        return False
    return os.path.normcase(common) == root_key


def remove_missing_under_root(
    store: ChunkStore,
    root: str,
    existing_paths: set[str],
) -> int:
    """Remove stale indexed files under one root in a single SQLite transaction.

    Contentless FTS5 tables cannot be repaired by blindly deleting raw chunks.
    Reuse ``ChunkStore._delete_chunks`` so every old FTS row is deleted with the
    exact filename/content values it was indexed with, while avoiding one
    connection and commit per missing file.

    Path membership uses resolved, ``normcase`` comparison so Windows drive/
    case normalization does not make a valid child path look unrelated to its
    configured root.
    """
    root_key = _path_key(root)
    existing_keys = {_path_key(path) for path in existing_paths}
    missing: list[tuple[int, str]] = []

    with store.connect() as conn:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        for row in rows:
            file_id = int(row["id"])
            path = str(row["path"])
            path_key = _path_key(path)
            if not _is_under_root(path_key, root_key):
                continue
            if path_key not in existing_keys:
                missing.append((file_id, path))

        for file_id, path in missing:
            store._delete_chunks(conn, path, file_id=file_id)
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    return len(missing)
