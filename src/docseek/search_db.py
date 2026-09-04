from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SearchResult:
    path: str
    filename: str
    snippet: str
    score: float


class SearchDatabase:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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
                    size INTEGER NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
                    path UNINDEXED,
                    filename,
                    content,
                    tokenize='unicode61'
                );
                """
            )

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
                INSERT INTO files(path, filename, extension, modified_time, size)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename,
                    extension=excluded.extension,
                    modified_time=excluded.modified_time,
                    size=excluded.size
                """,
                (path, filename, extension, modified_time, size),
            )
            conn.execute("DELETE FROM file_fts WHERE path = ?", (path,))
            conn.execute(
                "INSERT INTO file_fts(path, filename, content) VALUES (?, ?, ?)",
                (path, filename, content),
            )

    def search(self, query: str, limit: int = 100) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []

        # Treat whitespace-separated words as AND terms while keeping the
        # syntax deliberately simple for ordinary office users.
        terms = [term for term in query.split() if term]
        fts_query = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    path,
                    filename,
                    snippet(file_fts, 2, '[', ']', ' … ', 24),
                    bm25(file_fts, 1.5, 1.0)
                FROM file_fts
                WHERE file_fts MATCH ?
                ORDER BY bm25(file_fts, 1.5, 1.0)
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()

        return [SearchResult(*row) for row in rows]
