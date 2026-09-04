from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .chunks import DocumentChunk


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass(slots=True)
class ChunkSearchResult:
    path: str
    filename: str
    extension: str
    modified_time: float
    size: int
    location: str
    snippet: str
    score: float


class ChunkStore:
    """Location-aware FTS index with one file split into multiple bounded chunks."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._trigram_available = False
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, factory=_ClosingConnection)
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
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                    path UNINDEXED,
                    ordinal UNINDEXED,
                    location UNINDEXED,
                    filename,
                    content,
                    tokenize='unicode61 remove_diacritics 2',
                    prefix='2 3 4'
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts_cjk2 USING fts5(
                    path UNINDEXED,
                    ordinal UNINDEXED,
                    location UNINDEXED,
                    filename_tokens,
                    content_tokens,
                    tokenize='unicode61'
                );
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts_tri USING fts5(
                        path UNINDEXED,
                        ordinal UNINDEXED,
                        location UNINDEXED,
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

    def replace_document(
        self,
        *,
        path: str,
        filename: str,
        extension: str,
        modified_time: float,
        size: int,
        chunks: Iterable[DocumentChunk],
    ) -> int:
        count = 0
        filename_bigrams = self._cjk_bigrams(filename)
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
            self._delete_chunks(conn, path)
            for chunk in chunks:
                values = (path, chunk.ordinal, chunk.location, filename, chunk.content)
                conn.execute(
                    "INSERT INTO chunk_fts(path, ordinal, location, filename, content) VALUES (?, ?, ?, ?, ?)",
                    values,
                )
                conn.execute(
                    "INSERT INTO chunk_fts_cjk2(path, ordinal, location, filename_tokens, content_tokens) VALUES (?, ?, ?, ?, ?)",
                    (
                        path,
                        chunk.ordinal,
                        chunk.location,
                        filename_bigrams,
                        self._cjk_bigrams(chunk.content),
                    ),
                )
                if self._trigram_available:
                    conn.execute(
                        "INSERT INTO chunk_fts_tri(path, ordinal, location, filename, content) VALUES (?, ?, ?, ?, ?)",
                        values,
                    )
                count += 1
        return count

    def remove_document(self, path: str) -> None:
        with self.connect() as conn:
            self._delete_chunks(conn, path)
            conn.execute("DELETE FROM files WHERE path = ?", (path,))

    def _delete_chunks(self, conn: sqlite3.Connection, path: str) -> None:
        conn.execute("DELETE FROM chunk_fts WHERE path = ?", (path,))
        conn.execute("DELETE FROM chunk_fts_cjk2 WHERE path = ?", (path,))
        if self._trigram_available:
            conn.execute("DELETE FROM chunk_fts_tri WHERE path = ?", (path,))

    def remove_missing_under_root(self, root: str, existing_paths: set[str]) -> int:
        root_path = Path(root).resolve()
        with self.connect() as conn:
            rows = conn.execute("SELECT path FROM files").fetchall()
        missing: list[str] = []
        for row in rows:
            path = str(row["path"])
            candidate = Path(path)
            try:
                candidate.relative_to(root_path)
            except ValueError:
                continue
            if path not in existing_paths:
                missing.append(path)
        for path in missing:
            self.remove_document(path)
        return len(missing)

    @staticmethod
    def _build_fts_query(query: str) -> str:
        terms = [term.strip() for term in query.split() if term.strip()]
        escaped = [term.replace('"', '""') for term in terms]
        return " AND ".join(f'"{term}"' for term in escaped)

    @staticmethod
    def _cjk_only(query: str) -> str:
        return "".join(ch for ch in query if _CJK_RE.match(ch))

    def search(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        extension: str | None = None,
        path_contains: str | None = None,
    ) -> list[ChunkSearchResult]:
        query = query.strip()
        if not query:
            return []

        compact_cjk = self._cjk_only(query)
        is_short_cjk = compact_cjk and len(compact_cjk) <= 2 and compact_cjk == "".join(query.split())
        if is_short_cjk:
            table = "chunk_fts_cjk2"
            fts_query = f'"{compact_cjk}"'
            snippet_expr = "''"
            score_expr = "bm25(chunk_fts_cjk2, 0.0, 0.0, 0.0, 5.0, 1.0)"
        else:
            compact = "".join(query.split())
            use_tri = self._trigram_available and bool(_CJK_RE.search(query)) and len(compact) >= 3
            table = "chunk_fts_tri" if use_tri else "chunk_fts"
            fts_query = self._build_fts_query(query)
            snippet_expr = f"snippet({table}, 4, '[[HIT]]', '[[/HIT]]', ' … ', 36)"
            score_expr = f"bm25({table}, 0.0, 0.0, 0.0, 5.0, 1.0)"

        clauses = [f"{table} MATCH ?"]
        params: list[object] = [fts_query]
        if extension:
            clauses.append("f.extension = ?")
            params.append(extension)
        if path_contains:
            clauses.append("LOWER(f.path) LIKE ?")
            params.append(f"%{path_contains.casefold()}%")

        fetch_limit = max(limit * 5, 250)
        params.extend([fetch_limit, max(0, offset * 3)])
        sql = f"""
            SELECT
                f.path, f.filename, f.extension, f.modified_time, f.size,
                {table}.location AS location,
                {snippet_expr} AS snippet,
                {score_expr} AS score
            FROM {table}
            JOIN files f ON f.path = {table}.path
            WHERE {' AND '.join(clauses)}
            ORDER BY score ASC, f.modified_time DESC
            LIMIT ? OFFSET ?
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results: list[ChunkSearchResult] = []
        seen: set[str] = set()
        for row in rows:
            path = str(row["path"])
            if path in seen:
                continue
            seen.add(path)
            snippet = str(row["snippet"] or "")
            if is_short_cjk:
                snippet = self._plain_chunk_snippet(path, str(row["location"]), query)
            results.append(
                ChunkSearchResult(
                    path=path,
                    filename=str(row["filename"]),
                    extension=str(row["extension"]),
                    modified_time=float(row["modified_time"]),
                    size=int(row["size"]),
                    location=str(row["location"] or ""),
                    snippet=snippet,
                    score=float(row["score"]),
                )
            )
            if len(results) >= limit:
                break
        return results

    def _plain_chunk_snippet(self, path: str, location: str, query: str, radius: int = 52) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT content FROM chunk_fts WHERE path = ? AND location = ? LIMIT 1",
                (path, location),
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
