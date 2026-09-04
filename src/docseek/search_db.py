from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


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
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
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
                    tokenize='unicode61 remove_diacritics 2'
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
                conn.execute("DELETE FROM files WHERE path = ?", (path,))
        return len(missing)

    def save_index_root(self, root: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('index_root', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (root,),
            )

    def get_index_root(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='index_root'"
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

        fts_query = self._build_fts_query(query)
        params: list[object] = [fts_query]
        ext_clause = ""
        if extension:
            ext_clause = " AND f.extension = ?"
            params.append(extension)
        params.append(limit)

        sql = f"""
            SELECT
                f.path,
                f.filename,
                f.extension,
                f.modified_time,
                f.size,
                snippet(file_fts, 2, '[[HIT]]', '[[/HIT]]', ' … ', 32) AS snippet,
                bm25(file_fts, 0.0, 4.0, 1.0) AS score
            FROM file_fts
            JOIN files f ON f.path = file_fts.path
            WHERE file_fts MATCH ? {ext_clause}
            ORDER BY score ASC, f.modified_time DESC
            LIMIT ?
        """

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            SearchResult(
                path=str(row["path"]),
                filename=str(row["filename"]),
                extension=str(row["extension"]),
                modified_time=float(row["modified_time"]),
                size=int(row["size"]),
                snippet=str(row["snippet"] or ""),
                score=float(row["score"]),
            )
            for row in rows
        ]
