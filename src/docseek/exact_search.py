from __future__ import annotations

from .chunk_store import ChunkSearchResult, ChunkStore, SearchPage
from .structure_ranking import parse_structure_query


class ExactGroupedSearchEngine:
    """Exact file-level search with structure-aware ranking.

    FTS ranking is chunk-level while filename boosts are file-level. Metadata
    filters also apply to whole files. For broad single-term queries DocSeek
    materializes eligible file ids before scoring chunks; for multi-term AND
    queries, FTS is already selective enough that late metadata filtering avoids
    paying the materialization overhead. Both paths preserve exact semantics.

    Explicit ``page:N``, ``slide:N`` and ``sheet:name`` hints are schema-free:
    they are removed from the FTS text and add a strong boost to matching
    ``chunks.location`` values while the best chunk is selected.

    Ordinary queries also get a smaller automatic boost when their compact text
    appears in a semantic locator such as an Excel sheet name or an enriched
    document/slide title. This keeps explicit hints optional and lets structure
    improve ranking without changing the persisted database schema.
    """

    def __init__(self, store: ChunkStore) -> None:
        self.store = store

    @staticmethod
    def _structure_boost_expr() -> str:
        return """
            CASE
                WHEN :page_hint IS NOT NULL
                 AND REPLACE(c.location, ' ', '') = '第' || CAST(:page_hint AS TEXT) || '页'
                    THEN -3.0
                ELSE 0.0
            END
            + CASE
                WHEN :slide_hint IS NOT NULL
                 AND (
                     REPLACE(c.location, ' ', '') =
                         '幻灯片' || CAST(:slide_hint AS TEXT)
                     OR REPLACE(c.location, ' ', '') LIKE
                         '幻灯片' || CAST(:slide_hint AS TEXT) || '·标题%'
                 )
                    THEN -3.0
                ELSE 0.0
            END
            + CASE
                WHEN :sheet_hint IS NOT NULL
                 AND LOWER(REPLACE(c.location, ' ', '')) LIKE
                     LOWER('工作表' || REPLACE(:sheet_hint, ' ', '') || '·行%')
                    THEN -2.5
                ELSE 0.0
            END
        """

    @staticmethod
    def _automatic_structure_boost_expr() -> str:
        """Small intent boost for semantic structure already stored in location."""
        return """
            CASE
                WHEN :location_query <> ''
                 AND (
                     LOWER(REPLACE(c.location, ' ', '')) LIKE '工作表%'
                     OR INSTR(c.location, ' · 标题 ') > 0
                 )
                 AND INSTR(
                     LOWER(REPLACE(c.location, ' ', '')),
                     LOWER(:location_query)
                 ) > 0
                    THEN -1.25
                ELSE 0.0
            END
        """

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
            return self.store.browse_page(
                limit=limit,
                offset=offset,
                extension=extension,
                path_contains=path_contains,
                modified_after=modified_after,
                modified_before=modified_before,
                min_size=min_size,
                max_size=max_size,
            )

        structured = parse_structure_query(query)
        content_query = structured.text
        table, fts_query = self.store._select_index(content_query)
        score_expr = f"bm25({table}, 5.0, 1.0)"
        structure_boost_expr = self._structure_boost_expr()
        automatic_structure_boost_expr = self._automatic_structure_boost_expr()
        plain_query = self.store._plain_query_text(content_query)
        params: dict[str, object] = {
            "fts_query": fts_query,
            "raw_query": plain_query,
            "location_query": "".join(plain_query.split()),
            "page_hint": structured.hints.page,
            "slide_hint": structured.hints.slide,
            "sheet_hint": structured.hints.sheet,
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }

        metadata_clauses: list[str] = []
        self.store._append_metadata_filters(
            metadata_clauses,
            params,
            extension=extension,
            path_contains=path_contains,
            modified_after=modified_after,
            modified_before=modified_before,
            min_size=min_size,
            max_size=max_size,
        )
        query_terms = self.store._split_query_terms(content_query)
        use_metadata_prefilter = bool(metadata_clauses) and len(query_terms) <= 1
        if use_metadata_prefilter:
            eligible_cte = f"""
            eligible_files AS MATERIALIZED (
                SELECT f.id
                FROM files f
                WHERE {' AND '.join(metadata_clauses)}
            ),
            """
            eligible_join = "JOIN eligible_files ef ON ef.id = c.file_id"
            metadata_where = ""
        else:
            eligible_cte = ""
            eligible_join = ""
            metadata_where = (
                "WHERE " + " AND ".join(metadata_clauses)
                if metadata_clauses
                else ""
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
            WITH {eligible_cte}hits AS MATERIALIZED (
                SELECT
                    c.id AS chunk_id,
                    c.file_id AS file_id,
                    c.ordinal AS ordinal,
                    {score_expr}
                    + ({structure_boost_expr})
                    + ({automatic_structure_boost_expr}) AS chunk_score
                FROM {table}
                JOIN chunks c ON c.id = {table}.rowid
                {eligible_join}
                WHERE {table} MATCH :fts_query
            ),
            best_scores AS (
                SELECT file_id, MIN(chunk_score) AS chunk_score
                FROM hits
                GROUP BY file_id
            ),
            best_ordinals AS (
                SELECT
                    h.file_id,
                    b.chunk_score,
                    MIN(h.ordinal) AS ordinal
                FROM hits h
                JOIN best_scores b
                  ON b.file_id = h.file_id
                 AND b.chunk_score = h.chunk_score
                GROUP BY h.file_id, b.chunk_score
            ),
            winners AS (
                SELECT h.chunk_id, h.file_id, h.chunk_score
                FROM hits h
                JOIN best_ordinals b
                  ON b.file_id = h.file_id
                 AND b.chunk_score = h.chunk_score
                 AND b.ordinal = h.ordinal
            ),
            file_hits AS (
                SELECT
                    w.chunk_id,
                    w.file_id,
                    f.path,
                    f.filename,
                    f.extension,
                    f.modified_time,
                    f.size,
                    w.chunk_score + ({filename_boost_expr}) AS relevance_score
                FROM winners w
                JOIN files f ON f.id = w.file_id
                {metadata_where}
            ),
            paged AS (
                SELECT
                    chunk_id, file_id, path, filename, extension,
                    modified_time, size, relevance_score,
                    COUNT(*) OVER() AS total_count
                FROM file_hits
                ORDER BY relevance_score ASC, modified_time DESC, path ASC
                LIMIT :limit OFFSET :offset
            )
            SELECT
                paged.path,
                paged.filename,
                paged.extension,
                paged.modified_time,
                paged.size,
                chunks.location AS location,
                chunks.content AS raw_content,
                paged.relevance_score AS score,
                paged.total_count
            FROM paged
            JOIN chunks ON chunks.id = paged.chunk_id
            ORDER BY paged.relevance_score ASC, paged.modified_time DESC, paged.path ASC
        """

        with self.store.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            total_count = int(rows[0]["total_count"]) if rows else 0
            if not rows and int(params["offset"]) > 0:
                total_count = self.count_files(
                    query,
                    extension=extension,
                    path_contains=path_contains,
                    modified_after=modified_after,
                    modified_before=modified_before,
                    min_size=min_size,
                    max_size=max_size,
                )

        items = [
            ChunkSearchResult(
                path=str(row["path"]),
                filename=str(row["filename"]),
                extension=str(row["extension"]),
                modified_time=float(row["modified_time"]),
                size=int(row["size"]),
                location=str(row["location"] or ""),
                snippet=self.store._snippet_from_content(
                    self.store.decode_content(row["raw_content"]), content_query
                ),
                score=float(row["score"]),
            )
            for row in rows
        ]
        return SearchPage(items=items, total_count=total_count)

    def count_files(
        self,
        query: str,
        *,
        extension: str | None = None,
        path_contains: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
    ) -> int:
        query = query.strip()
        if not query:
            return self.store.browse_page(
                limit=1,
                extension=extension,
                path_contains=path_contains,
                modified_after=modified_after,
                modified_before=modified_before,
                min_size=min_size,
                max_size=max_size,
            ).total_count

        content_query = parse_structure_query(query).text
        table, fts_query = self.store._select_index(content_query)
        metadata_clauses: list[str] = []
        params: dict[str, object] = {"fts_query": fts_query}
        self.store._append_metadata_filters(
            metadata_clauses,
            params,
            extension=extension,
            path_contains=path_contains,
            modified_after=modified_after,
            modified_before=modified_before,
            min_size=min_size,
            max_size=max_size,
        )

        if metadata_clauses:
            sql = f"""
                WITH eligible_files AS MATERIALIZED (
                    SELECT f.id
                    FROM files f
                    WHERE {' AND '.join(metadata_clauses)}
                ),
                matched_files AS (
                    SELECT DISTINCT c.file_id
                    FROM {table}
                    JOIN chunks c ON c.id = {table}.rowid
                    JOIN eligible_files ef ON ef.id = c.file_id
                    WHERE {table} MATCH :fts_query
                )
                SELECT COUNT(*) AS n FROM matched_files
            """
        else:
            sql = f"""
                WITH matched_files AS (
                    SELECT DISTINCT c.file_id
                    FROM {table}
                    JOIN chunks c ON c.id = {table}.rowid
                    WHERE {table} MATCH :fts_query
                )
                SELECT COUNT(*) AS n FROM matched_files
            """
        with self.store.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)
