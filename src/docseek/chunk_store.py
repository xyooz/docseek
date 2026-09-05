from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .chunk_codec import decode_chunk_content, encode_chunk_content
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
    """Location-aware local FTS index.

    Raw chunk text is stored once in ``chunks``. Two contentless FTS5 indexes
    provide complementary retrieval paths:

    - ``chunk_index`` for normal unicode/Latin token search;
    - ``chunk_index_cjk2`` for Chinese substring search using overlapping
      bigrams and FTS phrase matching.

    Schema v4 removed the old trigram copy because a contiguous Chinese query
    of length >= 2 can be represented exactly as an overlapping-bigram phrase.
    Schema v5 introduced the integer ``files.id`` relationship. Schema v6
    removed the duplicate full path from every chunk. Schema v7 changes the
    logical raw-content format: new/updated chunks store a versioned zlib BLOB,
    while legacy TEXT rows remain readable. The FTS indexes still receive the
    original Unicode text, so search semantics and FTS rowids do not change.
    Existing v6 rows are intentionally not rewritten during startup; this avoids
    a large upgrade WAL and lets changed documents migrate lazily.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
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

            if previous_version < 3:
                self._create_v4_schema(conn)
                self._migrate_pre_v3_chunks(conn)
            elif previous_version < 4:
                self._migrate_v3_to_v4(conn)
            elif previous_version < 6:
                self._create_v4_schema(conn)
            else:
                self._create_v6_schema(conn)

            if previous_version < 6:
                self._ensure_v5_file_ids(conn)
                self._migrate_v5_to_v6(conn)
            else:
                self._create_v6_schema(conn)

            # v7 is deliberately a logical codec migration. SQLite's dynamic
            # typing lets the existing TEXT-affinity column hold compressed
            # BLOBs, so opening a large v6 database does not rewrite every row.
            mark_schema_current(conn)

    @staticmethod
    def _create_v4_schema(conn: sqlite3.Connection) -> None:
        """Create the legacy path-bearing layout needed by pre-v6 migrations."""
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
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_index_cjk2 USING fts5(
                filename_tokens,
                content_tokens,
                content='',
                tokenize='unicode61'
            );
            """
        )

    @staticmethod
    def _create_v6_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                location TEXT NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(file_id, ordinal)
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_file_id ON chunks(file_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_index USING fts5(
                filename,
                content,
                content='',
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_index_cjk2 USING fts5(
                filename_tokens,
                content_tokens,
                content='',
                tokenize='unicode61'
            );
            """
        )

    @staticmethod
    def _ensure_v5_file_ids(conn: sqlite3.Connection) -> None:
        """Backfill the integer relationship before the v6 table compaction."""
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
        }
        if "file_id" not in columns:
            if "path" not in columns:
                raise RuntimeError(
                    "索引缺少 file_id 与 path，无法安全升级到 v6。"
                )
            conn.execute("ALTER TABLE chunks ADD COLUMN file_id INTEGER")
            columns.add("file_id")

        if "path" in columns:
            conn.execute(
                """
                UPDATE chunks
                SET file_id = (
                    SELECT f.id FROM files f WHERE f.path = chunks.path
                )
                WHERE file_id IS NULL
                """
            )

        orphan_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE file_id IS NULL"
            ).fetchone()[0]
        )
        if orphan_count:
            raise RuntimeError(
                "索引升级到 v6 时发现无法映射到文件元数据的内容块："
                f"{orphan_count} 个。为避免静默丢失搜索结果，升级已中止。"
            )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_file_id ON chunks(file_id)"
        )

    def _migrate_v5_to_v6(self, conn: sqlite3.Connection) -> None:
        """Remove duplicated chunk paths while preserving chunk/FTS rowids."""
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
        }
        if "path" not in columns:
            self._create_v6_schema(conn)
            return

        orphan_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE file_id IS NULL"
            ).fetchone()[0]
        )
        if orphan_count:
            raise RuntimeError(
                "索引升级到 v6 前仍存在未映射内容块："
                f"{orphan_count} 个。升级已中止。"
            )

        old_count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        conn.execute("DROP TABLE IF EXISTS chunks_v6")
        conn.execute(
            """
            CREATE TABLE chunks_v6(
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                location TEXT NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(file_id, ordinal)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO chunks_v6(id, file_id, ordinal, location, content)
            SELECT id, file_id, ordinal, location, content
            FROM chunks
            ORDER BY id
            """
        )
        new_count = int(conn.execute("SELECT COUNT(*) FROM chunks_v6").fetchone()[0])
        if new_count != old_count:
            raise RuntimeError(
                "索引升级到 v6 时内容块数量不一致："
                f"迁移前 {old_count}，迁移后 {new_count}。升级已中止。"
            )

        conn.execute("DROP TABLE chunks")
        conn.execute("ALTER TABLE chunks_v6 RENAME TO chunks")
        conn.execute("CREATE INDEX idx_chunks_file_id ON chunks(file_id)")

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        """Rebuild only the compact inverted indexes; source files stay closed."""
        conn.execute("DROP TABLE IF EXISTS chunk_index")
        conn.execute("DROP TABLE IF EXISTS chunk_index_tri")
        self._create_v4_schema(conn)

        rows = conn.execute(
            """
            SELECT c.id, c.content, f.filename
            FROM chunks c
            JOIN files f ON f.path = c.path
            ORDER BY c.id
            """
        )
        for row in rows:
            chunk_id = int(row["id"])
            filename = str(row["filename"] or "")
            content = str(row["content"] or "")
            conn.execute(
                "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, ?, ?)",
                (chunk_id, filename, content),
            )

    def _migrate_pre_v3_chunks(self, conn: sqlite3.Connection) -> None:
        """Move v1/v2 chunk data into the single-copy legacy layout."""
        if not self._table_exists(conn, "chunk_fts"):
            return

        conn.execute("DELETE FROM chunks")
        conn.execute("INSERT INTO chunk_index(chunk_index) VALUES('delete-all')")
        conn.execute("INSERT INTO chunk_index_cjk2(chunk_index_cjk2) VALUES('delete-all')")

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

        conn.execute("DROP TABLE IF EXISTS chunk_lookup")
        conn.execute("DROP TABLE IF EXISTS chunk_fts_tri")
        conn.execute("DROP TABLE IF EXISTS chunk_fts_cjk2")
        conn.execute("DROP TABLE IF EXISTS chunk_fts")
        conn.execute("DROP TABLE IF EXISTS chunk_index_tri")

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

    @classmethod
    def _cjk_phrase(cls, text: str) -> str:
        tokens = cls._cjk_bigrams(text).split()
        if not tokens:
            return '""'
        escaped = " ".join(token.replace('"', '""') for token in tokens)
        return f'"{escaped}"'

    @staticmethod
    def decode_content(value: object) -> str:
        return decode_chunk_content(value)

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

    @staticmethod
    def _file_id_for_path(conn: sqlite3.Connection, path: str) -> int | None:
        row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        return int(row[0]) if row else None

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
            file_id = self._file_id_for_path(conn, path)
            if file_id is None:
                raise RuntimeError(f"无法为索引文件分配 file_id：{path}")

            self._delete_chunks(conn, path, file_id=file_id)
            for chunk in chunks:
                cursor = conn.execute(
                    """
                    INSERT INTO chunks(file_id, ordinal, location, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        chunk.ordinal,
                        chunk.location,
                        encode_chunk_content(chunk.content),
                    ),
                )
                chunk_id = int(cursor.lastrowid)
                self._insert_fts_rows(conn, chunk_id, filename, chunk.content)
                count += 1
        return count

    def remove_document(self, path: str) -> None:
        with self.connect() as conn:
            file_id = self._file_id_for_path(conn, path)
            self._delete_chunks(conn, path, file_id=file_id)
            conn.execute("DELETE FROM files WHERE path = ?", (path,))

    @staticmethod
    def _contentless_delete(
        conn: sqlite3.Connection,
        table: str,
        rowid: int,
        first: str,
        second: str,
    ) -> None:
        conn.execute(
            f"INSERT INTO {table}({table}, rowid, {('filename_tokens' if table.endswith('_cjk2') else 'filename')}, {('content_tokens' if table.endswith('_cjk2') else 'content')}) "
            "VALUES ('delete', ?, ?, ?)",
            (rowid, first, second),
        )

    def _delete_chunks(
        self,
        conn: sqlite3.Connection,
        path: str,
        *,
        file_id: int | None = None,
    ) -> None:
        if file_id is None:
            file_id = self._file_id_for_path(conn, path)
        if file_id is None:
            return

        rows = conn.execute(
            """
            SELECT c.id, c.content, f.filename
            FROM chunks c
            JOIN files f ON f.id = c.file_id
            WHERE c.file_id = ?
            ORDER BY c.id
            """,
            (file_id,),
        ).fetchall()

        for row in rows:
            chunk_id = int(row["id"])
            content = decode_chunk_content(row["content"])
            filename = str(row["filename"] or "")
            self._contentless_delete(conn, "chunk_index", chunk_id, filename, content)
            self._contentless_delete(
                conn,
                "chunk_index_cjk2",
                chunk_id,
                self._cjk_bigrams(filename),
                self._cjk_bigrams(content),
            )

        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))

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
    def _split_query_terms(query: str) -> list[tuple[str, bool]]:
        """Return (text, quoted) terms while tolerating unmatched quotes."""
        terms: list[tuple[str, bool]] = []
        current: list[str] = []
        in_quotes = False
        token_quoted = False

        def flush() -> None:
            nonlocal current, token_quoted
            if current:
                terms.append(("".join(current), token_quoted))
                current = []
                token_quoted = False

        for char in query.strip():
            if char == '"':
                if not current:
                    token_quoted = True
                in_quotes = not in_quotes
                continue
            if char.isspace() and not in_quotes:
                flush()
                continue
            current.append(char)
        flush()
        return terms

    @classmethod
    def _build_fts_query(cls, query: str) -> str:
        rendered: list[str] = []
        for text, _quoted in cls._split_query_terms(query):
            escaped = text.replace('"', '""')
            rendered.append(f'"{escaped}"')
        return " AND ".join(rendered)

    @staticmethod
    def _cjk_only(query: str) -> str:
        return "".join(ch for ch in query if _CJK_RE.match(ch))

    @classmethod
    def _plain_query_text(cls, query: str) -> str:
        return " ".join(text for text, _quoted in cls._split_query_terms(query))

    def _select_index(self, query: str) -> tuple[str, str]:
        terms = self._split_query_terms(query)
        if terms and all(
            text and self._cjk_only(text) == "".join(text.split())
            for text, _quoted in terms
        ):
            return (
                "chunk_index_cjk2",
                " AND ".join(self._cjk_phrase("".join(text.split())) for text, _quoted in terms),
            )
        return "chunk_index", self._build_fts_query(query)

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

        table, fts_query = self._select_index(query)
        score_expr = f"bm25({table}, 5.0, 1.0)"
        clauses = [f"{table} MATCH :fts_query"]
        params: dict[str, object] = {
            "fts_query": fts_query,
            "raw_query": self._plain_query_text(query),
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
                    f.id AS file_id,
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
                JOIN files f ON f.id = c.file_id
                WHERE {' AND '.join(clauses)}
            ),
            ranked AS (
                SELECT
                    *,
                    bm25_score + filename_boost AS relevance_score,
                    ROW_NUMBER() OVER (
                        PARTITION BY file_id
                        ORDER BY bm25_score + filename_boost ASC, ordinal ASC
                    ) AS file_rank
                FROM hits
            ),
            file_hits AS (
                SELECT
                    chunk_id, file_id, path, filename, extension, modified_time, size,
                    ordinal, location, relevance_score
                FROM ranked
                WHERE file_rank = 1
            ),
            paged AS (
                SELECT
                    chunk_id, file_id, path, filename, extension, modified_time, size,
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
                            f.id AS file_id,
                            c.ordinal AS ordinal,
                            {score_expr} AS bm25_score,
                            {filename_boost_expr} AS filename_boost
                        FROM {table}
                        JOIN chunks c ON c.id = {table}.rowid
                        JOIN files f ON f.id = c.file_id
                        WHERE {' AND '.join(clauses)}
                    ),
                    ranked AS (
                        SELECT
                            file_id,
                            ROW_NUMBER() OVER (
                                PARTITION BY file_id
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
                snippet=self._snippet_from_content(row["raw_content"], query),
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

    @classmethod
    def _snippet_from_content(cls, content: object, query: str, radius: int = 52) -> str:
        decoded = decode_chunk_content(content)
        terms = [text for text, _quoted in cls._split_query_terms(query) if text]
        candidates = sorted(set(terms), key=len, reverse=True)
        folded = decoded.casefold()

        positions = [
            (folded.find(candidate.casefold()), candidate)
            for candidate in candidates
        ]
        positions = [(pos, term) for pos, term in positions if pos >= 0]
        if not positions:
            return decoded[: radius * 2].replace("\n", " ")

        position, anchor = min(positions, key=lambda item: item[0])
        start = max(0, position - radius)
        end = min(len(decoded), position + len(anchor) + radius)
        snippet = decoded[start:end].replace("\n", " ")

        if candidates:
            pattern = re.compile(
                "|".join(re.escape(term) for term in candidates),
                flags=re.IGNORECASE,
            )
            snippet = pattern.sub(lambda match: f"[[HIT]]{match.group(0)}[[/HIT]]", snippet)

        return ("… " if start else "") + snippet + (" …" if end < len(decoded) else "")
