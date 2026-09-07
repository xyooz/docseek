from __future__ import annotations

import os
from pathlib import Path

from .chunk_store import ChunkStore
from .document_types import KNOWN_DOCUMENT_EXTENSIONS


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

    Extraction state is removed in the same transaction. This prevents stale
    NO_TEXT/FAILED state from surviving after a source file is deleted.

    The hot path first compares the canonical path strings already produced by
    ``DirectoryIndexer``. Healthy reconciliations therefore avoid resolving the
    same thousands of paths a second time. If an exact string mismatch appears
    (legacy data, case differences, symlink aliases or a genuinely missing
    file), comparison keys are built lazily and the previous resolved/normcase
    semantics are preserved for that reconciliation.
    """
    root_key = _path_key(root)
    existing_exact = set(existing_paths)
    existing_keys: set[str] | None = None
    missing: list[tuple[int, str]] = []

    with store.connect() as conn:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        for row in rows:
            file_id = int(row["id"])
            path = str(row["path"])

            # Files indexed by current DocSeek builds are stored using the same
            # canonical string that full discovery puts in ``seen_paths``. This
            # is the overwhelmingly common unchanged-scan case and needs no
            # filesystem work at all.
            if path in existing_exact:
                continue

            path_key = _path_key(path)
            if not _is_under_root(path_key, root_key):
                continue

            # A mismatch may simply be a Windows case/normalization difference
            # or a legacy/symlink path. Build the expensive resolved-key set only
            # after such a mismatch is actually observed.
            if existing_keys is None:
                existing_keys = {_path_key(existing) for existing in existing_exact}
            if path_key not in existing_keys:
                missing.append((file_id, path))

        for file_id, path in missing:
            store._delete_chunks(conn, path, file_id=file_id)
            conn.execute("DELETE FROM extraction_state WHERE path = ?", (path,))
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    return len(missing)


def remove_disabled_extensions(
    store: ChunkStore,
    enabled_extensions: set[str] | frozenset[str],
) -> int:
    """Remove indexed content for formats the user has disabled.

    This is global rather than root-scoped so a paused directory cannot keep
    stale XML or other disabled content searchable after the setting changes.
    Source files are never touched.
    """
    enabled = {str(extension).lower() for extension in enabled_extensions}
    removed_paths: list[str] = []

    with store.connect() as conn:
        rows = conn.execute("SELECT id, path, extension FROM files").fetchall()
        for row in rows:
            if str(row["extension"]).lower() in enabled:
                continue
            file_id = int(row["id"])
            path = str(row["path"])
            store._delete_chunks(conn, path, file_id=file_id)
            conn.execute("DELETE FROM extraction_state WHERE path = ?", (path,))
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            removed_paths.append(path)

        for table in ("file_fts", "file_fts_cjk2", "file_fts_tri"):
            if store._table_exists(conn, table):
                conn.executemany(
                    f"DELETE FROM {table} WHERE path = ?",
                    ((path,) for path in removed_paths),
                )

        if store._table_exists(conn, "index_issues"):
            issue_rows = conn.execute("SELECT path FROM index_issues").fetchall()
            disabled_issue_paths = [
                str(row["path"])
                for row in issue_rows
                if (
                    Path(str(row["path"])).suffix.lower()
                    in KNOWN_DOCUMENT_EXTENSIONS - enabled
                )
            ]
            conn.executemany(
                "DELETE FROM index_issues WHERE path = ?",
                ((path,) for path in disabled_issue_paths),
            )

    return len(removed_paths)
