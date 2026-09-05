from __future__ import annotations

from .chunk_store import ChunkSearchResult, ChunkStore, SearchPage


class ExactGroupedSearchEngine:
    """Exact file-level search using compact grouped minima.

    ``ChunkStore.search_page`` uses ``ROW_NUMBER() OVER`` across every matching
    chunk. This engine preserves the same exact ordering rule with two grouped
    minima while keeping the materialized hit set intentionally narrow:

    1. minimum relevance score per integer ``file_id``;
    2. minimum chunk ordinal among chunks tied at that score.

    File metadata and raw chunk text are joined only after the hit stream has
    collapsed to one winning chunk per file. This avoids copying long paths,
    filenames and other metadata into every matching chunk row, which matters
    for broad queries over large office-document indexes.
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
                    c.file_id AS file_id,
                    c.ordinal AS ordinal,
                    ({score_expr}) + ({filename_boost_expr}) AS relevance_score
                FROM {table}
                JOIN chunks c ON c.id = {table}.rowid
                JOIN files f ON f.id = c.file_id
                WHERE {' AND '.join(clauses)}
            ),
            best_scores AS (
                SELECT file_id, MIN(relevance_score) AS relevance_score
                FROM hits
                GROUP BY file_id
            ),
            best_ordinals AS (
                SELECT
                    h.file_id,
                    b.relevance_score,
                    MIN(h.ordinal) AS ordinal
                FROM hits h
                JOIN best_scores b
                  ON b.file_id = h.file_id
                 AND b.relevance_score = h.relevance_score
                GROUP BY h.file_id, b.relevance_score
            ),
            file_hits AS (
                SELECT h.chunk_id, h.file_id, h.relevance_score
                FROM hits h
                JOIN best_ordinals b
                  ON b.file_id = h.file_id
                 AND b.relevance_score = h.relevance_score
                 AND b.ordinal = h.ordinal
            ),
            paged AS (
                SELECT
                    h.chunk_id,
                    h.file_id,
                    h.relevance_score,
                    f.path,
                    f.filename,
                    f.extension,
                    f.modified_time,
                    f.size,
                    COUNT(*) OVER() AS total_count
                FROM file_hits h
                JOIN files f ON f.id = h.file_id
                ORDER BY h.relevance_score ASC, f.modified_time DESC, f.path ASC
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
            SELECT COUNT(DISTINCT c.file_id) AS n
            FROM {table}
            JOIN chunks c ON c.id = {table}.rowid
            JOIN files f ON f.id = c.file_id
            WHERE {' AND '.join(clauses)}
        """
        with self.store.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)
