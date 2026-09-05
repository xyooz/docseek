from __future__ import annotations

from pathlib import Path

from .chunk_store import ChunkStore


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
    """
    root_path = Path(root).resolve()
    missing: list[tuple[int, str]] = []

    with store.connect() as conn:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        for row in rows:
            file_id = int(row["id"])
            path = str(row["path"])
            candidate = Path(path)
            try:
                candidate.relative_to(root_path)
            except ValueError:
                continue
            if path not in existing_paths:
                missing.append((file_id, path))

        for file_id, path in missing:
            store._delete_chunks(conn, path, file_id=file_id)
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    return len(missing)
