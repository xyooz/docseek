from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


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
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-32768")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
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
                # Some enterprise Python builds may ship SQLite without the
                # trigram tokenizer. DocSeek remains usable with unicode61.
                self._trigram_available = False

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
            conn.execute(
                "UPDATE files SET last_error = ? WHERE path = ?",
                (error, path),
            )

    def remove_missing_under_root(self, root: str, existing_paths: set[str]) -> int:
        root_path = Path(root)
        with self.connect() as conn:
            rows = conn.execute("SELECT path FROM files").fetchall()
            missing: list[str] = []
            for row in rows:
                candidate = Path(str(row["path"]))
                try:
                    candidate.relative_to(root_path)
                except ValueError:
                    continue
                if str(candidate) not in existing_paths:
                    missing.append(str(candidate))

            for path in missing:
                conn.execute("DELETE FROM file_fts WHERE path = ?", (path,))
                if self._trigram_available:
                    conn.execute("DELETE FROM file_fts_tri WHERE path = ?", (path,))
                conn.execute("DELETE FROM files WHERE path = ?", (path,))
        return len(missing)

    def add_index_root(self, root: str) -> None:
        resolved = str(Path(root).resolve())
        roots = self.get_index_roots()
        if resolved not in roots:
            roots.append(resolved)
        self._set_setting("index_roots", json.dumps(roots, ensure_ascii=False))
        # Keep the old key for compatibility with early DocSeek versions.
        self._set_setting("index_root", resolved)

    def remove_index_root(self, root: str) -> None:
        resolved = str(Path(root).resolve())
        roots = [item for item in self.get_index_roots() if item != resolved]
        self._set_setting("index_roots", json.dumps(roots, ensure_ascii=False))

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
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
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

    def search(
        self,
        query: str,
        *,
        limit: int = 100,
        extension: str | None = None,
    ) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []

        # For Chinese continuous text, trigram substring matching gives much
        # better recall than unicode61, which otherwise treats long CJK runs as
        # a single token. Short queries stay on the conventional FTS index.
        compact = "".join(query.split())
        use_trigram = (
            self._trigram_available
            and self._is_cjk_query(query)
            and len(compact) >= 3
        )
        table = "file_fts_tri" if use_trigram else "file_fts"
        fts_query = self._build_fts_query(query)

        params: list[object] = [fts_query]
        ext_clause = ""
        if extension:
            ext_clause = " AND f.extension = ?"
            params.append(extension)
        params.append(limit)

        # Filename receives a stronger BM25 weight than body text. The path is
        # unindexed and is not allowed to influence relevance.
        sql = f"""
            SELECT
                f.path,
                f.filename,
                f.extension,
                f.modified_time,
                f.size,
                snippet({table}, 2, '[[HIT]]', '[[/HIT]]', ' … ', 36) AS snippet,
                bm25({table}, 0.0, 5.0, 1.0) AS score
            FROM {table}
            JOIN files f ON f.path = {table}.path
            WHERE {table} MATCH ? {ext_clause}
            ORDER BY score ASC, f.modified_time DESC
            LIMIT ?
        """

        try:
            with self.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            if not use_trigram:
                raise
            # Fallback protects older/locked-down SQLite builds and malformed
            # trigram edge cases without making the search box feel fragile.
            return self._search_unicode(query, limit=limit, extension=extension)

        return [self._row_to_result(row) for row in rows]

    def _search_unicode(
        self,
        query: str,
        *,
        limit: int,
        extension: str | None,
    ) -> list[SearchResult]:
        fts_query = self._build_fts_query(query)
        params: list[object] = [fts_query]
        ext_clause = ""
        if extension:
            ext_clause = " AND f.extension = ?"
            params.append(extension)
        params.append(limit)
        sql = f"""
            SELECT f.path, f.filename, f.extension, f.modified_time, f.size,
                   snippet(file_fts, 2, '[[HIT]]', '[[/HIT]]', ' … ', 36) AS snippet,
                   bm25(file_fts, 0.0, 5.0, 1.0) AS score
            FROM file_fts
            JOIN files f ON f.path = file_fts.path
            WHERE file_fts MATCH ? {ext_clause}
            ORDER BY score ASC, f.modified_time DESC
            LIMIT ?
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
