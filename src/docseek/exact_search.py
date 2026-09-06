from __future__ import annotations

from .chunk_store import ChunkSearchResult, ChunkStore, SearchPage
from .search_sort import (
    SORT_RELEVANCE,
    browse_order_clause,
    search_order_clause,
    validate_sort_mode,
)
from .structure_ranking import parse_structure_query


class ExactGroupedSearchEngine:
    """Exact file-level search with late metadata and structure-aware ranking.

    FTS ranking is chunk-level, while filename boosts and supported metadata
    filters are file-level. Keeping metadata filtering after exact chunk scoring
    has proven the most stable behavior across Windows CI runners; experimental
    prefilter variants remain useful for benchmarking but are not part of the
    production path.

    Explicit ``page:N``, ``slide:N`` and ``sheet:name`` hints are schema-free:
    they are removed from the FTS text and add a strong boost to matching
    ``chunks.location`` values while the best chunk is selected.

    Ordinary queries also get a smaller automatic boost when their text appears
    in a semantic locator such as an Excel sheet name or an enriched
    document/slide title. Besides exact compact phrase matches, multi-term
    queries can receive a weaker boost when every query term is present in the
    semantic locator even if extra words appear between those terms. This keeps
    explicit hints optional and lets structure improve ranking without changing
    the persisted database schema.

    File ordering is selected from a closed allow-list. Relevance remains the
    default; modified-time and filename modes only change file order, while the
    best matching chunk/snippet inside each file is still chosen by relevance.
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
    def _structure_query_terms(plain_query: str, *, limit: int = 8) -> list[str]:
        """Return bounded, de-duplicated compact terms for semantic locators.

        The exact compact-query match remains the strongest automatic structure
        signal. These terms only support a weaker fallback for queries such as
        ``risk audit`` matching ``Risk quarterly audit``. Bounding the term list
        keeps generated SQL predictable for interactive searches.
        """
        terms: list[str] = []
        seen: set[str] = set()
        for raw_term in plain_query.split():
            term = "".join(raw_term.split()).strip()
            folded = term.casefold()
            if not term or folded in seen:
                continue
            seen.add(folded)
            terms.append(term)
            if len(terms) >= limit:
                break
        return terms

    @staticmethod
    def _automatic_structure_boost_expr(term_count: int) -> str:
        """Small intent boost for semantic structure already stored in location.

        Generic page/block coordinates are ignored. Exact compact phrase matches
        receive the established boost; a multi-term fallback receives a smaller
        boost only when every bounded query term occurs in the same semantic
        locator. This avoids rewarding a title for matching just one generic
        word from a longer query.
        """
        semantic_guard = """
            (
                LOWER(REPLACE(c.location, ' ', '')) LIKE '工作表%'
                OR INSTR(c.location, ' · 标题 ') > 0
            )
        """
        term_fallback = ""
        if term_count >= 2:
            all_terms = " AND ".join(
                "INSTR(LOWER(REPLACE(c.location, ' ', '')), "
                f"LOWER(:location_term_{index})) > 0"
                for index in range(term_count)
            )
            term_fallback = f"""
                WHEN {semantic_guard}
                 AND ({all_terms})
                    THEN -0.9
            """
        return f"""
            CASE
                WHEN :location_query <> ''
                 AND {semantic_guard}
                 AND INSTR(
                     LOWER(REPLACE(c.location, ' ', '')),
                     LOWER(:location_query)
                 ) > 0
                    THEN -1.25
                {term_fallback}
                ELSE 0.0
            END
        """

    def _browse_page(
        self,
        *,
        sort_mode: str,
        limit: int,
        offset: int,
        extension: str | None,
        path_contains: str | None,
        modified_after: float | None,
        modified_before: float | None,
        min_size: int | None,
        max_size: int | None,
    ) -> SearchPage:
        order_clause = browse_order_clause(sort_mode)
        clauses: list[str] = []
        params: dict[str, object] = {
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        self.store._append_metadata_filters(
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
                COUNT(*) OVER() AS total_count
            FROM files f
            {where}
            ORDER BY {order_clause}
            LIMIT :limit OFFSET :offset
        """
        with self.store.connect() as conn:
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

        return SearchPage(
            items=[
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
            ],
            total_count=total_count,
        )

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
        sort_mode: str = SORT_RELEVANCE,
    ) -> SearchPage:
        sort_mode = validate_sort_mode(sort_mode)
        query = query.strip()
        if not query:
            return self._browse_page(
                sort_mode=sort_mode,
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
        plain_query = self.store._plain_query_text(content_query)
        structure_terms = self._structure_query_terms(plain_query)
        automatic_structure_boost_expr = self._automatic_structure_boost_expr(
            len(structure_terms)
        )
        order_clause = search_order_clause(sort_mode)
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
        for index, term in enumerate(structure_terms):
            params[f"location_term_{index}"] = term

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
            WITH hits AS MATERIALIZED (
                SELECT
                    c.id AS chunk_id,
                    c.file_id AS file_id,
                    c.ordinal AS ordinal,
                    {score_expr}
                    + ({structure_boost_expr})
                    + ({automatic_structure_boost_expr}) AS chunk_score
                FROM {table}
                JOIN chunks c ON c.id = {table}.rowid
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
                ORDER BY {order_clause}
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
            ORDER BY {order_clause}
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
            return self._browse_page(
                sort_mode=SORT_RELEVANCE,
                limit=1,
                offset=0,
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
        metadata_where = (
            "WHERE " + " AND ".join(metadata_clauses)
            if metadata_clauses
            else ""
        )
        sql = f"""
            WITH matched_files AS (
                SELECT DISTINCT c.file_id
                FROM {table}
                JOIN chunks c ON c.id = {table}.rowid
                WHERE {table} MATCH :fts_query
            )
            SELECT COUNT(*) AS n
            FROM matched_files m
            JOIN files f ON f.id = m.file_id
            {metadata_where}
        """
        with self.store.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)
