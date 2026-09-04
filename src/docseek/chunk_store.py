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
        modified_after: float | None = None,
        modified_before: float | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
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

        clauses = [f"{table} MATCH :fts_query"]
        params: dict[str, object] = {
            "fts_query": fts_query,
            "raw_query": query,
            "limit": max(1, limit),
            "offset": max(0, offset),
        }
        if extension:
            clauses.append("f.extension = :extension")
            params["extension"] = extension
        if path_contains:
            clauses.append("LOWER(f.path) LIKE :path_contains")
            params["path_contains"] = f"%{path_contains.casefold()}%"
        if modified_after is not None:
            clauses.append("f.modified_time >= :modified_after")
            params["modified_after"] = float(modified_after)
        if modified_before is not None:
            clauses.append("f.modified_time < :modified_before")
            params["modified_before"] = float(modified_before)
        if min_size is not None:
            clauses.append("f.size >= :min_size")
            params["min_size"] = max(0, int(min_size))
        if max_size is not None:
            clauses.append("f.size <= :max_size")
            params["max_size"] = max(0, int(max_size))

        filename_boost_expr = """
            CASE
                WHEN LOWER(SUBSTR(f.filename, 1, LENGTH(f.filename) - LENGTH(f.extension))) = LOWER(:raw_query)
                    THEN -8.0
                WHEN LOWER(f.filename) LIKE LOWER(:raw_query) || '%'
                    THEN -4.0
                WHEN INSTR(LOWER(f.filename), LOWER(:raw_query)) > 0
                    THEN -2.0
                ELSE 0.0
            END
        """

        sql = f"""
            WITH hits AS (
                SELECT
                    f.path,
                    f.filename,
                    f.extension,
                    f.modified_time,
                    f.size,
                    CAST({table}.ordinal AS INTEGER) AS ordinal,
                    {table}.location AS location,
                    {snippet_expr} AS snippet,
                    {score_expr} AS bm25_score,
                    {filename_boost_expr} AS filename_boost
                FROM {table}
                JOIN files f ON f.path = {table}.path
                WHERE {' AND '.join(clauses)}
            ),
            ranked AS (
                SELECT
                    *,
                    bm25_score + filename_boost AS relevance_score,
                    ROW_NUMBER() OVER (
                        PARTITION BY path
                        ORDER BY bm25_score + filename_boost ASC, ordinal ASC
                    ) AS file_rank
                FROM hits
            )
            SELECT
                path, filename, extension, modified_time, size,
                location, snippet, relevance_score AS score
            FROM ranked
            WHERE file_rank = 1
            ORDER BY relevance_score ASC, modified_time DESC, path ASC
            LIMIT :limit OFFSET :offset
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results: list[ChunkSearchResult] = []
        for row in rows:
            snippet = str(row["snippet"] or "")
            if is_short_cjk:
                snippet = self._plain_chunk_snippet(
                    str(row["path"]),
                    str(row["location"]),
                    query,
                )
            results.append(
                ChunkSearchResult(
                    path=str(row["path"]),
                    filename=str(row["filename"]),
                    extension=str(row["extension"]),
                    modified_time=float(row["modified_time"]),
                    size=int(row["size"]),
                    location=str(row["location"] or ""),
                    snippet=snippet,
                    score=float(row["score"]),
                )
            )
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
