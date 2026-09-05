from __future__ import annotations

from .chunk_store import ChunkSearchResult, ChunkStore, SearchPage


class ExactGroupedSearchEngine:
    """Exact file-level search with late file-metadata evaluation.

    FTS ranking is chunk-level, while filename boosts and all supported metadata
    filters are file-level. Those file-level values are constant for every chunk
    of the same document, so evaluating them before choosing the best chunk only
    repeats work.

    This engine therefore:

    1. matches FTS rows and keeps only ``chunk_id/file_id/ordinal/bm25``;
    2. chooses the minimum BM25 chunk per file, breaking ties by ordinal;
    3. joins ``files`` exactly once per surviving file;
    4. applies metadata filters and the filename boost at file level;
    5. sorts and paginates the exact file-level result set.

    Moving file-level work after chunk collapse does not change search semantics:
    metadata filters accept or reject the whole file, and filename boost is the
    same constant for every chunk in a file, so neither can change which chunk is
    the file's best content hit.
    """

    def __init__(self, store: ChunkStore) -> None:
        self.store = store

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

        table, fts_query = self.store._select_index(query)
        score_expr = f"bm25({table}, 5.0, 1.0)"
        params: dict[str, object] = {
            "fts_query": fts_query,
            "raw_query": self.store._plain_query_text(query),
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
                    {score_expr} AS bm25_score
                FROM {table}
                JOIN chunks c ON c.id = {table}.rowid
                WHERE {table} MATCH :fts_query
            ),
            best_scores AS (
                SELECT file_id, MIN(bm25_score) AS bm25_score
                FROM hits
                GROUP BY file_id
            ),
            best_ordinals AS (
                SELECT
                    h.file_id,
                    b.bm25_score,
                    MIN(h.ordinal) AS ordinal
                FROM hits h
                JOIN best_scores b
                  ON b.file_id = h.file_id
                 AND b.bm25_score = h.bm25_score
                GROUP BY h.file_id, b.bm25_score
            ),
            winners AS (
                SELECT h.chunk_id, h.file_id, h.bm25_score
                FROM hits h
                JOIN best_ordinals b
                  ON b.file_id = h.file_id
                 AND b.bm25_score = h.bm25_score
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
                    w.bm25_score + ({filename_boost_expr}) AS relevance_score
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
                snippet=self.store._snippet_from_content(str(row["raw_content"] or ""), query),
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

        table, fts_query = self.store._select_index(query)
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
