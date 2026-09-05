from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .chunks import DocumentChunk
from .schema import ensure_schema_compatible, mark_schema_current


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


@dataclass(slots=True)
class SearchPage:
    items: list[ChunkSearchResult]
    total_count: int


class ChunkStore:
    """Location-aware FTS index with one file split into bounded chunks.

    Raw chunk text is stored once in the ordinary ``chunks`` table. The FTS5
    tables are contentless inverted indexes that share the same chunk rowid.
    This avoids keeping a private copy of the full text in every tokenizer
    index while preserving unicode, short-CJK and trigram retrieval paths.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._trigram_available = False
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-32768")
        return conn

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            previous_version = ensure_schema_compatible(conn)
            self._create_v3_schema(conn)
            if previous_version < 3:
                self._migrate_pre_v3_chunks(conn)
            mark_schema_current(conn)

    def _create_v3_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                location TEXT NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(path, ordinal)
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);

            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_index USING fts5(
                filename,
                content,
                content='',
                tokenize='unicode61 remove_diacritics 2',
                prefix='2 3 4'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_index_cjk2 USING fts5(
                filename_tokens,
                content_tokens,
                content='',
                tokenize='unicode61'
            );
            """
        )
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_index_tri USING fts5(
                    filename,
                    content,
                    content='',
                    tokenize='trigram case_sensitive 0'
                )
                """
            )
            self._trigram_available = True
        except sqlite3.OperationalError:
            self._trigram_available = False

    def _migrate_pre_v3_chunks(self, conn: sqlite3.Connection) -> None:
        """Move v1/v2 chunk data into the single-copy v3 layout.

        The old base FTS table contains the original text, so migration can
        rebuild the new inverted indexes without reopening or reparsing source
        Office/PDF files.
        """
        if not self._table_exists(conn, "chunk_fts"):
            return

        # A schema migration is transactional. Clearing the new tables makes
        # this deterministic if a development database contains an unfinished
        # pre-v3 experiment.
        conn.execute("DELETE FROM chunks")
        conn.execute("INSERT INTO chunk_index(chunk_index) VALUES('delete-all')")
        conn.execute("INSERT INTO chunk_index_cjk2(chunk_index_cjk2) VALUES('delete-all')")
        if self._trigram_available:
            conn.execute("INSERT INTO chunk_index_tri(chunk_index_tri) VALUES('delete-all')")

        rows = conn.execute(
            """
            SELECT rowid, path, CAST(ordinal AS INTEGER) AS ordinal,
                   location, filename, content
            FROM chunk_fts
            ORDER BY rowid
            """
        )
        for row in rows:
            chunk_id = int(row["rowid"])
            path = str(row["path"])
            ordinal = int(row["ordinal"])
            location = str(row["location"] or "")
            filename = str(row["filename"] or "")
            content = str(row["content"] or "")
            conn.execute(
                "INSERT INTO chunks(id, path, ordinal, location, content) VALUES (?, ?, ?, ?, ?)",
                (chunk_id, path, ordinal, location, content),
            )
            self._insert_fts_rows(conn, chunk_id, filename, content)

        # Remove the old contentful copies only after the new layout is built.
        conn.execute("DROP TABLE IF EXISTS chunk_lookup")
        conn.execute("DROP TABLE IF EXISTS chunk_fts_tri")
        conn.execute("DROP TABLE IF EXISTS chunk_fts_cjk2")
        conn.execute("DROP TABLE IF EXISTS chunk_fts")

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

    def _insert_fts_rows(
        self,
        conn: sqlite3.Connection,
        chunk_id: int,
        filename: str,
        content: str,
    ) -> None:
        conn.execute(
            "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, ?, ?)",
            (chunk_id, filename, content),
        )
        conn.execute(
            "INSERT INTO chunk_index_cjk2(rowid, filename_tokens, content_tokens) VALUES (?, ?, ?)",
            (chunk_id, self._cjk_bigrams(filename), self._cjk_bigrams(content)),
        )
        if self._trigram_available:
            conn.execute(
                "INSERT INTO chunk_index_tri(rowid, filename, content) VALUES (?, ?, ?)",
                (chunk_id, filename, content),
            )

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
                cursor = conn.execute(
                    "INSERT INTO chunks(path, ordinal, location, content) VALUES (?, ?, ?, ?)",
                    (path, chunk.ordinal, chunk.location, chunk.content),
                )
                chunk_id = int(cursor.lastrowid)
                self._insert_fts_rows(conn, chunk_id, filename, chunk.content)
                count += 1
        return count

    def remove_document(self, path: str) -> None:
        with self.connect() as conn:
            self._delete_chunks(conn, path)
            conn.execute("DELETE FROM files WHERE path = ?", (path,))

    @staticmethod
    def _contentless_delete(
        conn: sqlite3.Connection,
        table: str,
        rowid: int,
        first: str,
        second: str,
    ) -> None:
        # Standard contentless FTS5 tables support deletion through the
        # special 'delete' command. Supplying the original indexed values keeps
        # this compatible with SQLite builds older than contentless-delete.
        conn.execute(
            f"INSERT INTO {table}({table}, rowid, {('filename_tokens' if table.endswith('_cjk2') else 'filename')}, {('content_tokens' if table.endswith('_cjk2') else 'content')}) "
            "VALUES ('delete', ?, ?, ?)",
            (rowid, first, second),
        )

    def _delete_chunks(self, conn: sqlite3.Connection, path: str) -> None:
        rows = conn.execute(
            """
            SELECT c.id, c.content, f.filename
            FROM chunks c
            JOIN files f ON f.path = c.path
            WHERE c.path = ?
            ORDER BY c.id
            """,
            (path,),
        ).fetchall()
        for row in rows:
            chunk_id = int(row["id"])
            content = str(row["content"] or "")
            filename = str(row["filename"] or "")
            self._contentless_delete(conn, "chunk_index", chunk_id, filename, content)
            self._contentless_delete(
                conn,
                "chunk_index_cjk2",
                chunk_id,
                self._cjk_bigrams(filename),
                self._cjk_bigrams(content),
            )
            if self._trigram_available:
                self._contentless_delete(conn, "chunk_index_tri", chunk_id, filename, content)
        conn.execute("DELETE FROM chunks WHERE path = ?", (path,))

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

    @staticmethod
    def _append_metadata_filters(
        clauses: list[str],
        params: dict[str, object],
        *,
        extension: str | None,
        path_contains: str | None,
        modified_after: float | None,
        modified_before: float | None,
        min_size: int | None,
        max_size: int | None,
    ) -> None:
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

    def browse_page(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        extension: str | None = None,
        path_contains: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
    ) -> SearchPage:
        clauses: list[str] = []
        params: dict[str, object] = {
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        self._append_metadata_filters(
            clauses,
            params,
            extension=extension,
            path_contains=path_contains,
            modified_after=modified_after,
            modified_before=modified_before,
            min_size=min_size,
            max_size=max_size,
        )
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT
                f.path, f.filename, f.extension, f.modified_time, f.size,
                '' AS location, '' AS snippet, 0.0 AS score,
                COUNT(*) OVER() AS total_count
            FROM files f
            {where}
            ORDER BY f.modified_time DESC, LOWER(f.filename) ASC, f.path ASC
            LIMIT :limit OFFSET :offset
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            total_count = int(rows[0]["total_count"]) if rows else 0
            if not rows and int(params["offset"]) > 0:
                count_sql = f"SELECT COUNT(*) AS n FROM files f {where}"
                count_params = {
                    key: value
                    for key, value in params.items()
                    if key not in {"limit", "offset"}
                }
                total_count = int(conn.execute(count_sql, count_params).fetchone()["n"])

        items = [
            ChunkSearchResult(
                path=str(row["path"]),
                filename=str(row["filename"]),
                extension=str(row["extension"]),
                modified_time=float(row["modified_time"]),
                size=int(row["size"]),
                location="",
                snippet="",
                score=0.0,
            )
            for row in rows
        ]
        return SearchPage(items=items, total_count=total_count)

    def browse(
        self,
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
        return self.browse_page(
            limit=limit,
            offset=offset,
            extension=extension,
            path_contains=path_contains,
            modified_after=modified_after,
            modified_before=modified_before,
            min_size=min_size,
            max_size=max_size,
        ).items

    def search_page(
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
    ) -> SearchPage:
        query = query.strip()
        if not query:
            return self.browse_page(
                limit=limit,
                offset=offset,
                extension=extension,
                path_contains=path_contains,
                modified_after=modified_after,
                modified_before=modified_before,
                min_size=min_size,
                max_size=max_size,
            )

        compact_cjk = self._cjk_only(query)
        is_short_cjk = bool(
            compact_cjk
            and len(compact_cjk) <= 2
            and compact_cjk == "".join(query.split())
        )

        if is_short_cjk:
            table = "chunk_index_cjk2"
            fts_query = f'"{compact_cjk}"'
        else:
            compact = "".join(query.split())
            use_tri = (
                self._trigram_available
                and bool(_CJK_RE.search(query))
                and len(compact) >= 3
            )
            table = "chunk_index_tri" if use_tri else "chunk_index"
            fts_query = self._build_fts_query(query)

        score_expr = f"bm25({table}, 5.0, 1.0)"
        clauses = [f"{table} MATCH :fts_query"]
        params: dict[str, object] = {
            "fts_query": fts_query,
            "raw_query": query,
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        self._append_metadata_filters(
            clauses,
            params,
            extension=extension,
            path_contains=path_contains,
            modified_after=modified_after,
            modified_before=modified_before,
            min_size=min_size,
            max_size=max_size,
        )

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
                    c.id AS chunk_id,
                    f.path,
                    f.filename,
                    f.extension,
                    f.modified_time,
                    f.size,
                    c.ordinal AS ordinal,
                    c.location AS location,
                    {score_expr} AS bm25_score,
                    {filename_boost_expr} AS filename_boost
                FROM {table}
                JOIN chunks c ON c.id = {table}.rowid
                JOIN files f ON f.path = c.path
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
            ),
            file_hits AS (
                SELECT
                    chunk_id, path, filename, extension, modified_time, size,
                    ordinal, location, relevance_score
                FROM ranked
                WHERE file_rank = 1
            ),
            paged AS (
                SELECT
                    chunk_id, path, filename, extension, modified_time, size,
                    ordinal, location, relevance_score,
                    COUNT(*) OVER() AS total_count
                FROM file_hits
                ORDER BY relevance_score ASC, modified_time DESC, path ASC
                LIMIT :limit OFFSET :offset
            )
            SELECT
                paged.path, paged.filename, paged.extension,
                paged.modified_time, paged.size, paged.location,
                chunks.content AS raw_content,
                paged.relevance_score AS score, paged.total_count
            FROM paged
            JOIN chunks ON chunks.id = paged.chunk_id
            ORDER BY paged.relevance_score ASC, paged.modified_time DESC, paged.path ASC
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            total_count = int(rows[0]["total_count"]) if rows else 0
            if not rows and int(params["offset"]) > 0:
                count_sql = f"""
                    WITH hits AS (
                        SELECT
                            f.path,
                            c.ordinal AS ordinal,
                            {score_expr} AS bm25_score,
                            {filename_boost_expr} AS filename_boost
                        FROM {table}
                        JOIN chunks c ON c.id = {table}.rowid
                        JOIN files f ON f.path = c.path
                        WHERE {' AND '.join(clauses)}
                    ),
                    ranked AS (
                        SELECT
                            path,
                            ROW_NUMBER() OVER (
                                PARTITION BY path
                                ORDER BY bm25_score + filename_boost ASC, ordinal ASC
                            ) AS file_rank
                        FROM hits
                    )
                    SELECT COUNT(*) AS n FROM ranked WHERE file_rank = 1
                """
                count_params = {
                    key: value
                    for key, value in params.items()
                    if key not in {"limit", "offset"}
                }
                total_count = int(conn.execute(count_sql, count_params).fetchone()["n"])

        items = [
            ChunkSearchResult(
                path=str(row["path"]),
                filename=str(row["filename"]),
                extension=str(row["extension"]),
                modified_time=float(row["modified_time"]),
                size=int(row["size"]),
                location=str(row["location"] or ""),
                snippet=self._snippet_from_content(str(row["raw_content"] or ""), query),
                score=float(row["score"]),
            )
            for row in rows
        ]
        return SearchPage(items=items, total_count=total_count)

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
        return self.search_page(
            query,
            limit=limit,
            offset=offset,
            extension=extension,
            path_contains=path_contains,
            modified_after=modified_after,
            modified_before=modified_before,
            min_size=min_size,
            max_size=max_size,
        ).items

    @staticmethod
    def _snippet_from_content(content: str, query: str, radius: int = 52) -> str:
        cleaned_query = query.strip().strip('"')
        terms = [term.strip('"') for term in query.split() if term.strip('"')]
        candidates = [cleaned_query] + terms if cleaned_query else terms
        folded = content.casefold()

        positions = [
            (folded.find(candidate.casefold()), candidate)
            for candidate in candidates
            if candidate
        ]
        positions = [(pos, term) for pos, term in positions if pos >= 0]
        if not positions:
            return content[: radius * 2].replace("\n", " ")

        position, anchor = min(positions, key=lambda item: item[0])
        start = max(0, position - radius)
        end = min(len(content), position + len(anchor) + radius)
        snippet = content[start:end].replace("\n", " ")

        highlight_terms = sorted({term for term in terms if term}, key=len, reverse=True)
        if cleaned_query and cleaned_query not in highlight_terms:
            highlight_terms.insert(0, cleaned_query)
        if highlight_terms:
            pattern = re.compile(
                "|".join(re.escape(term) for term in highlight_terms),
                flags=re.IGNORECASE,
            )
            snippet = pattern.sub(lambda match: f"[[HIT]]{match.group(0)}[[/HIT]]", snippet)

        return ("… " if start else "") + snippet + (" …" if end < len(content) else "")
