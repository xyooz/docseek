from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from .schema import ensure_schema_compatible
from .sqlite_runtime import current_schema_objects, ensure_wal_mode


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_READY_OBJECTS = frozenset({"files", "settings", "file_fts", "file_fts_cjk2"})


class _ClosingConnection(sqlite3.Connection):
    """sqlite3 connection whose context manager also closes the file handle."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass(slots=True)
class SearchResult:
    path: str
    filename: str
    extension: str
    modified_time: float
    size: int
    snippet: str
    score: float


class SearchDatabase:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._trigram_available = False
        self._preflight_schema_version()

        # Index workers are created repeatedly during watcher updates. Once the
        # v10 database is fully initialized, reopening it must be a read-only
        # schema probe rather than another round of WAL/schema initialization.
        objects = current_schema_objects(self.db_path)
        if objects is not None and _READY_OBJECTS.issubset(objects):
            self._trigram_available = "file_fts_tri" in objects
            return

        self._init_schema()

    def _preflight_schema_version(self) -> None:
        """Reject newer indexes before any compatibility table or WAL mutation."""
        if not self.db_path.exists():
            return
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            ensure_schema_compatible(conn)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
            factory=_ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-32768")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            # journal_mode is database-wide state. Only bootstrap/migration is
            # allowed to change it; current-schema runtime reopens skip this
            # method entirely.
            ensure_wal_mode(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    modified_time REAL NOT NULL,
                    size INTEGER NOT NULL,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
                    path UNINDEXED,
                    filename,
                    content,
                    tokenize='unicode61 remove_diacritics 2',
                    prefix='2 3 4'
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS file_fts_cjk2 USING fts5(
                    path UNINDEXED,
                    filename_tokens,
                    content_tokens,
                    tokenize='unicode61'
                );

                CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension);
                CREATE INDEX IF NOT EXISTS idx_files_modified_time ON files(modified_time);
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(files)").fetchall()
            }
            if "last_error" not in columns:
                conn.execute("ALTER TABLE files ADD COLUMN last_error TEXT")

            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS file_fts_tri USING fts5(
                        path UNINDEXED,
                        filename,
                        content,
                        tokenize='trigram case_sensitive 0'
                    )
                    """
                )
                self._trigram_available = True
            except sqlite3.OperationalError:
                self._trigram_available = False

    @staticmethod
    def _cjk_bigrams(text: str) -> str:
        tokens: list[str] = []
        for match in _CJK_RUN_RE.finditer(text):
            run = match.group(0)
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        return " ".join(tokens)

    def backfill_aux_indexes(self) -> bool:
        changed = False
        with self.connect() as conn:
            base_count = int(conn.execute("SELECT COUNT(*) FROM file_fts").fetchone()[0])
            cjk2_count = int(conn.execute("SELECT COUNT(*) FROM file_fts_cjk2").fetchone()[0])
            if cjk2_count != base_count:
                rows = conn.execute("SELECT path, filename, content FROM file_fts").fetchall()
                conn.execute("DELETE FROM file_fts_cjk2")
                conn.executemany(
                    "INSERT INTO file_fts_cjk2(path, filename_tokens, content_tokens) VALUES (?, ?, ?)",
                    [
                        (
                            str(row["path"]),
                            self._cjk_bigrams(str(row["filename"] or "")),
                            self._cjk_bigrams(str(row["content"] or "")),
                        )
                        for row in rows
                    ],
                )
                changed = True

            if self._trigram_available:
                tri_count = int(conn.execute("SELECT COUNT(*) FROM file_fts_tri").fetchone()[0])
                if tri_count != base_count:
                    conn.execute("DELETE FROM file_fts_tri")
                    conn.execute(
                        "INSERT INTO file_fts_tri(path, filename, content) "
                        "SELECT path, filename, content FROM file_fts"
                    )
                    changed = True
        return changed

    def upsert_document(
        self,
        *,
        path: str,
        filename: str,
        extension: str,
        modified_time: float,
        size: int,
        content: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO files(path, filename, extension, modified_time, size, last_error)
                VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename,
                    extension=excluded.extension,
                    modified_time=excluded.modified_time,
                    size=excluded.size,
                    last_error=NULL
                """,
                (path, filename, extension, modified_time, size),
            )
            conn.execute("DELETE FROM file_fts WHERE path = ?", (path,))
            conn.execute(
                "INSERT INTO file_fts(path, filename, content) VALUES (?, ?, ?)",
                (path, filename, content),
            )
            conn.execute("DELETE FROM file_fts_cjk2 WHERE path = ?", (path,))
            conn.execute(
                "INSERT INTO file_fts_cjk2(path, filename_tokens, content_tokens) VALUES (?, ?, ?)",
                (path, self._cjk_bigrams(filename), self._cjk_bigrams(content)),
            )
            if self._trigram_available:
                conn.execute("DELETE FROM file_fts_tri WHERE path = ?", (path,))
                conn.execute(
                    "INSERT INTO file_fts_tri(path, filename, content) VALUES (?, ?, ?)",
                    (path, filename, content),
                )

    def is_unchanged(self, path: str, *, modified_time: float, size: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT modified_time, size FROM files WHERE path = ?",
                (path,),
            ).fetchone()
        if row is None:
            return False
        return float(row["modified_time"]) == float(modified_time) and int(row["size"]) == int(size)

    def record_index_error(self, path: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE files SET last_error = ? WHERE path = ?", (error, path))

    def remove_document(self, path: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM file_fts WHERE path = ?", (path,))
            conn.execute("DELETE FROM file_fts_cjk2 WHERE path = ?", (path,))
            if self._trigram_available:
                conn.execute("DELETE FROM file_fts_tri WHERE path = ?", (path,))
            conn.execute("DELETE FROM files WHERE path = ?", (path,))

    def _paths_under_root(self, root: str) -> list[str]:
        root_path = Path(root).resolve()
        with self.connect() as conn:
            rows = conn.execute("SELECT path FROM files").fetchall()
        selected: list[str] = []
        for row in rows:
            candidate = Path(str(row["path"]))
            try:
                candidate.relative_to(root_path)
            except ValueError:
                continue
            selected.append(str(candidate))
        return selected

    def purge_root(self, root: str) -> int:
        paths = self._paths_under_root(root)
        for path in paths:
            self.remove_document(path)
        return len(paths)

    def remove_missing_under_root(self, root: str, existing_paths: set[str]) -> int:
        missing = [path for path in self._paths_under_root(root) if path not in existing_paths]
        for path in missing:
            self.remove_document(path)
        return len(missing)

    def add_index_root(self, root: str) -> None:
        resolved = str(Path(root).resolve())
        roots = self.get_index_roots()
        if resolved not in roots:
            roots.append(resolved)
        self._set_setting("index_roots", json.dumps(roots, ensure_ascii=False))
        self._set_setting("index_root", resolved)

    def remove_index_root(self, root: str) -> None:
        resolved = str(Path(root).resolve())
        roots = [item for item in self.get_index_roots() if item != resolved]
        self._set_setting("index_roots", json.dumps(roots, ensure_ascii=False))
        current_legacy = self._get_setting("index_root")
        if current_legacy == resolved:
            self._set_setting("index_root", roots[-1] if roots else "")

    def get_index_roots(self) -> list[str]:
        raw = self._get_setting("index_roots")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return [str(item) for item in data if item]
            except json.JSONDecodeError:
                pass
        legacy = self._get_setting("index_root")
        return [legacy] if legacy else []

    def add_excluded_path(self, path: str) -> None:
        resolved = str(Path(path).resolve())
        paths = self.get_excluded_paths()
        if resolved not in paths:
            paths.append(resolved)
        self._set_setting("excluded_paths", json.dumps(paths, ensure_ascii=False))

    def remove_excluded_path(self, path: str) -> None:
        resolved = str(Path(path).resolve())
        paths = [item for item in self.get_excluded_paths() if item != resolved]
        self._set_setting("excluded_paths", json.dumps(paths, ensure_ascii=False))

    def get_excluded_paths(self) -> list[str]:
        raw = self._get_setting("excluded_paths")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in data if item] if isinstance(data, list) else []

    def set_max_file_size_mb(self, value: int) -> None:
        value = max(1, min(int(value), 4096))
        self._set_setting("max_file_size_mb", str(value))

    def get_max_file_size_mb(self) -> int:
        raw = self._get_setting("max_file_size_mb")
        try:
            return max(1, min(int(raw), 4096)) if raw is not None else 200
        except ValueError:
            return 200

    def save_index_root(self, root: str) -> None:
        self.add_index_root(root)

    def get_index_root(self) -> str | None:
        roots = self.get_index_roots()
        return roots[-1] if roots else None

    def _set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _get_setting(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def count_files(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()
        return int(row["n"])

    @staticmethod
    def _build_fts_query(query: str) -> str:
        terms = [term.strip() for term in query.split() if term.strip()]
        escaped = [term.replace('"', '""') for term in terms]
        return " AND ".join(f'"{term}"' for term in escaped)

    @staticmethod
    def _is_cjk_query(query: str) -> bool:
        return bool(_CJK_RE.search(query))

    @staticmethod
    def _cjk_only_compact(query: str) -> str:
        return "".join(ch for ch in query if _CJK_RE.match(ch))

    @staticmethod
    def _append_filters(
        params: list[object],
        *,
        extension: str | None,
        path_contains: str | None,
    ) -> str:
        clauses: list[str] = []
        if extension:
            clauses.append("f.extension = ?")
            params.append(extension)
        if path_contains:
            clauses.append("f.path LIKE ? ESCAPE '\\'")
            escaped = path_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        return " AND " + " AND ".join(clauses) if clauses else ""

    def search(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        extension: str | None = None,
        path_contains: str | None = None,
    ) -> list[SearchResult]:
        query = query.strip()
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        if not query:
            return []

        compact_cjk = self._cjk_only_compact(query)
        if compact_cjk and len(compact_cjk) <= 2 and compact_cjk == "".join(query.split()):
            return self._search_cjk_short(
                compact_cjk,
                limit=limit,
                offset=offset,
                extension=extension,
                path_contains=path_contains,
            )

        compact = "".join(query.split())
        use_trigram = self._trigram_available and self._is_cjk_query(query) and len(compact) >= 3
        table = "file_fts_tri" if use_trigram else "file_fts"
        fts_query = self._build_fts_query(query)

        params: list[object] = [fts_query]
        filter_clause = self._append_filters(
            params,
            extension=extension,
            path_contains=path_contains,
        )
        params.extend([limit, offset])

        sql = f"""
            SELECT
                f.path, f.filename, f.extension, f.modified_time, f.size,
                snippet({table}, 2, '[[HIT]]', '[[/HIT]]', ' … ', 36) AS snippet,
                bm25({table}, 0.0, 5.0, 1.0) AS score
            FROM {table}
            JOIN files f ON f.path = {table}.path
            WHERE {table} MATCH ? {filter_clause}
            ORDER BY score ASC, f.modified_time DESC
            LIMIT ? OFFSET ?
        """

        try:
            with self.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            if not use_trigram:
                raise
            return self._search_unicode(
                query,
                limit=limit,
                offset=offset,
                extension=extension,
                path_contains=path_contains,
            )
        return [self._row_to_result(row) for row in rows]

    def _search_cjk_short(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        extension: str | None,
        path_contains: str | None,
    ) -> list[SearchResult]:
        params: list[object] = [f'"{query}"']
        filter_clause = self._append_filters(
            params,
            extension=extension,
            path_contains=path_contains,
        )
        params.extend([limit, offset])

        sql = f"""
            SELECT
                f.path, f.filename, f.extension, f.modified_time, f.size,
                '' AS snippet,
                bm25(file_fts_cjk2, 0.0, 5.0, 1.0) AS score
            FROM file_fts_cjk2
            JOIN files f ON f.path = file_fts_cjk2.path
            WHERE file_fts_cjk2 MATCH ? {filter_clause}
            ORDER BY score ASC, f.modified_time DESC
            LIMIT ? OFFSET ?
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results = [self._row_to_result(row) for row in rows]
        return [replace(item, snippet=self._plain_snippet(item.path, query)) for item in results]

    def _plain_snippet(self, path: str, query: str, radius: int = 52) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT content FROM file_fts WHERE path = ? LIMIT 1",
                (path,),
            ).fetchone()
        if not row:
            return ""
        content = str(row["content"] or "")
        position = content.casefold().find(query.casefold())
        if position < 0:
            return content[: radius * 2].replace("\n", " ")
        start = max(0, position - radius)
        end = min(len(content), position + len(query) + radius)
        snippet = content[start:end].replace("\n", " ")
        highlighted = snippet.replace(query, f"[[HIT]]{query}[[/HIT]]")
        return ("… " if start else "") + highlighted + (" …" if end < len(content) else "")

    def _search_unicode(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        extension: str | None,
        path_contains: str | None,
    ) -> list[SearchResult]:
        fts_query = self._build_fts_query(query)
        params: list[object] = [fts_query]
        filter_clause = self._append_filters(
            params,
            extension=extension,
            path_contains=path_contains,
        )
        params.extend([limit, offset])
        sql = f"""
            SELECT f.path, f.filename, f.extension, f.modified_time, f.size,
                   snippet(file_fts, 2, '[[HIT]]', '[[/HIT]]', ' … ', 36) AS snippet,
                   bm25(file_fts, 0.0, 5.0, 1.0) AS score
            FROM file_fts
            JOIN files f ON f.path = file_fts.path
            WHERE file_fts MATCH ? {filter_clause}
            ORDER BY score ASC, f.modified_time DESC
            LIMIT ? OFFSET ?
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_result(row) for row in rows]

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> SearchResult:
        return SearchResult(
            path=str(row["path"]),
            filename=str(row["filename"]),
            extension=str(row["extension"]),
            modified_time=float(row["modified_time"]),
            size=int(row["size"]),
            snippet=str(row["snippet"] or ""),
            score=float(row["score"]),
        )
