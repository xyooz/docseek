from __future__ import annotations

from .chunk_store import ChunkSearchResult, ChunkStore, SearchPage


class MetadataPrefilterSearchEngine:
    """Exact search that applies selective file metadata before FTS ranking.

    The production query starts from the FTS MATCH result and then joins file
    metadata. That is ideal for ordinary keyword search, but it can do needless
    work when a query narrows the scope heavily (for example ``ext:pdf`` plus a
    recent date range). This experimental engine materializes the eligible file
    and chunk ids first, then probes FTS only for those chunks.

    It preserves the same BM25, filename boosts, file-level collapse, tie-break
    and pagination semantics as ``ChunkStore.search_page``. It is deliberately
    kept separate until 10k/50k benchmarks show where the crossover point is.
    """

    def __init__(self, store: ChunkStore) -> None:
        self.store = store

    @staticmethod
    def _has_prefilter(
        *,
        extension: str | None,
        path_contains: str | None,
        modified_after: float | None,
        modified_before: float | None,
        min_size: int | None,
        max_size: int | None,
    ) -> bool:
        return any(
            value is not None
            for value in (
                extension,
                path_contains,
                modified_after,
                modified_before,
                min_size,
                max_size,
            )
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
    ) -> SearchPage:
        query = query.strip()
        if not query or not self._has_prefilter(
            extension=extension,
            path_contains=path_contains,
            modified_after=modified_after,
            modified_before=modified_before,
            min_size=min_size,
            max_size=max_size,
        ):
            return self.store.search_page(
                query,
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
        metadata_where = " AND ".join(metadata_clauses) or "1"

        filename_boost_expr = """
            CASE
                WHEN LOWER(SUBSTR(ec.filename, 1, LENGTH(ec.filename) - LENGTH(ec.extension))) = LOWER(:raw_query)
                    THEN -8.0
                WHEN LOWER(ec.filename) LIKE LOWER(:raw_query) || '%'
                    THEN -4.0
                WHEN INSTR(LOWER(ec.filename), LOWER(:raw_query)) > 0
                    THEN -2.0
                ELSE 0.0
            END
        """

        sql = f"""
            WITH eligible_files AS MATERIALIZED (
                SELECT f.id, f.path, f.filename, f.extension, f.modified_time, f.size
                FROM files f
                WHERE {metadata_where}
            ),
            eligible_chunks AS MATERIALIZED (
                SELECT
                    c.id AS chunk_id,
                    c.ordinal,
                    c.location,
                    ef.path,
                    ef.filename,
                    ef.extension,
                    ef.modified_time,
                    ef.size
                FROM eligible_files ef
                JOIN chunks c ON c.path = ef.path
            ),
            hits AS (
                SELECT
                    ec.chunk_id,
                    ec.path,
                    ec.filename,
                    ec.extension,
                    ec.modified_time,
                    ec.size,
                    ec.ordinal,
                    ec.location,
                    bm25({table}, 5.0, 1.0) AS bm25_score,
                    {filename_boost_expr} AS filename_boost
                FROM eligible_chunks ec
                CROSS JOIN {table}
                WHERE {table}.rowid = ec.chunk_id
                  AND {table} MATCH :fts_query
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
                paged.path,
                paged.filename,
                paged.extension,
                paged.modified_time,
                paged.size,
                paged.location,
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

        return SearchPage(
            items=[
                ChunkSearchResult(
                    path=str(row["path"]),
                    filename=str(row["filename"]),
                    extension=str(row["extension"]),
                    modified_time=float(row["modified_time"]),
                    size=int(row["size"]),
                    location=str(row["location"] or ""),
                    snippet=self.store._snippet_from_content(
                        str(row["raw_content"] or ""), query
                    ),
                    score=float(row["score"]),
                )
                for row in rows
            ],
            total_count=total_count,
        )

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
        params: dict[str, object] = {"fts_query": fts_query}
        clauses: list[str] = []
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
        where = " AND ".join(clauses) or "1"
        sql = f"""
            WITH eligible_files AS MATERIALIZED (
                SELECT f.id, f.path
                FROM files f
                WHERE {where}
            ),
            eligible_chunks AS MATERIALIZED (
                SELECT c.id AS chunk_id, ef.path
                FROM eligible_files ef
                JOIN chunks c ON c.path = ef.path
            )
            SELECT COUNT(DISTINCT ec.path) AS n
            FROM eligible_chunks ec
            CROSS JOIN {table}
            WHERE {table}.rowid = ec.chunk_id
              AND {table} MATCH :fts_query
        """
        with self.store.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)
