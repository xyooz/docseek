from __future__ import annotations

from .chunk_store import ChunkSearchResult, ChunkStore, SearchPage


class ExactGroupedSearchEngine:
    """Exact file-level search without a per-hit ROW_NUMBER window sort.

    ``ChunkStore.search_page`` currently uses ``ROW_NUMBER() OVER
    (PARTITION BY path ORDER BY score, ordinal)`` to select the best chunk for
    every file. This experimental engine preserves the exact same rule using
    two grouped minima:

    1. minimum relevance score per file;
    2. minimum chunk ordinal among chunks tied at that score.

    It remains exact and supports deep pagination. The separate engine makes it
    possible to benchmark and verify equivalence before changing production
    search SQL.
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
        clauses = [f"{table} MATCH :fts_query"]
        params: dict[str, object] = {
            "fts_query": fts_query,
            "raw_query": self.store._plain_query_text(query),
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
                    f.path,
                    f.filename,
                    f.extension,
                    f.modified_time,
                    f.size,
                    c.ordinal AS ordinal,
                    c.location AS location,
                    ({score_expr}) + ({filename_boost_expr}) AS relevance_score
                FROM {table}
                JOIN chunks c ON c.id = {table}.rowid
                JOIN files f ON f.path = c.path
                WHERE {' AND '.join(clauses)}
            ),
            best_scores AS (
                SELECT path, MIN(relevance_score) AS relevance_score
                FROM hits
                GROUP BY path
            ),
            best_ordinals AS (
                SELECT
                    h.path,
                    b.relevance_score,
                    MIN(h.ordinal) AS ordinal
                FROM hits h
                JOIN best_scores b
                  ON b.path = h.path
                 AND b.relevance_score = h.relevance_score
                GROUP BY h.path, b.relevance_score
            ),
            file_hits AS (
                SELECT
                    h.chunk_id,
                    h.path,
                    h.filename,
                    h.extension,
                    h.modified_time,
                    h.size,
                    h.ordinal,
                    h.location,
                    h.relevance_score
                FROM hits h
                JOIN best_ordinals b
                  ON b.path = h.path
                 AND b.relevance_score = h.relevance_score
                 AND b.ordinal = h.ordinal
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
        clauses = [f"{table} MATCH :fts_query"]
        params: dict[str, object] = {"fts_query": fts_query}
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
        sql = f"""
            SELECT COUNT(DISTINCT f.path) AS n
            FROM {table}
            JOIN chunks c ON c.id = {table}.rowid
            JOIN files f ON f.path = c.path
            WHERE {' AND '.join(clauses)}
        """
        with self.store.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)
