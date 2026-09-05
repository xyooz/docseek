from __future__ import annotations

from dataclasses import dataclass

from .chunk_store import ChunkSearchResult, ChunkStore


@dataclass(slots=True)
class ProgressiveSearchPage:
    """Fast first-page result before an optional exact count finishes."""

    items: list[ChunkSearchResult]
    exact_total: int | None
    exhausted: bool
    candidates_scanned: int


class ProgressiveSearchEngine:
    """Retrieve a small ranked candidate set before doing global aggregation.

    Exact ``ChunkStore.search_page()`` remains the source of truth for deep
    pagination. This engine is aimed at interactive first-paint latency: FTS5
    orders matching chunks by its optimized hidden ``rank`` column and LIMITs
    the candidate stream before DocSeek collapses chunks to files.
    """

    def __init__(self, store: ChunkStore) -> None:
        self.store = store

    @staticmethod
    def _filename_boost(filename: str, extension: str, raw_query: str) -> float:
        query = raw_query.casefold()
        if not query:
            return 0.0
        stem = filename[:-len(extension)] if extension and filename.casefold().endswith(extension.casefold()) else filename
        folded_name = filename.casefold()
        if stem.casefold() == query:
            return -8.0
        if folded_name.startswith(query):
            return -4.0
        if query in folded_name:
            return -2.0
        return 0.0

    def search_topk(
        self,
        query: str,
        *,
        limit: int = 100,
        extension: str | None = None,
        path_contains: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        candidate_multiplier: int = 8,
        max_candidates: int = 32768,
    ) -> ProgressiveSearchPage:
        query = query.strip()
        limit = max(1, min(int(limit), 500))
        if not query:
            page = self.store.browse_page(
                limit=limit,
                extension=extension,
                path_contains=path_contains,
                modified_after=modified_after,
                modified_before=modified_before,
                min_size=min_size,
                max_size=max_size,
            )
            return ProgressiveSearchPage(
                items=page.items,
                exact_total=page.total_count,
                exhausted=len(page.items) >= page.total_count,
                candidates_scanned=len(page.items),
            )

        table, fts_query = self.store._select_index(query)
        raw_query = self.store._plain_query_text(query)
        clauses = [f"{table} MATCH :fts_query", "rank MATCH 'bm25(5.0, 1.0)'"]
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

        candidate_limit = max(limit, limit * max(1, int(candidate_multiplier)))
        candidate_limit = min(candidate_limit, max(limit, int(max_candidates)))
        rows = []
        best_by_path: dict[str, tuple[float, int, object]] = {}
        exhausted = False

        while True:
            query_params = dict(params)
            query_params["candidate_limit"] = candidate_limit
            sql = f"""
                SELECT
                    c.id AS chunk_id,
                    c.ordinal AS ordinal,
                    c.location AS location,
                    f.path,
                    f.filename,
                    f.extension,
                    f.modified_time,
                    f.size,
                    rank AS bm25_score
                FROM {table}
                JOIN chunks c ON c.id = {table}.rowid
                JOIN files f ON f.path = c.path
                WHERE {' AND '.join(clauses)}
                ORDER BY rank ASC
                LIMIT :candidate_limit
            """
            with self.store.connect() as conn:
                rows = conn.execute(sql, query_params).fetchall()

            best_by_path.clear()
            for row in rows:
                filename = str(row["filename"])
                ext = str(row["extension"])
                score = float(row["bm25_score"]) + self._filename_boost(filename, ext, raw_query)
                ordinal = int(row["ordinal"])
                path = str(row["path"])
                existing = best_by_path.get(path)
                if existing is None or (score, ordinal) < (existing[0], existing[1]):
                    best_by_path[path] = (score, ordinal, row)

            exhausted = len(rows) < candidate_limit
            if len(best_by_path) >= limit or exhausted or candidate_limit >= max_candidates:
                break
            candidate_limit = min(max_candidates, candidate_limit * 2)

        selected = sorted(
            best_by_path.values(),
            key=lambda entry: (
                entry[0],
                -float(entry[2]["modified_time"]),
                str(entry[2]["path"]),
            ),
        )[:limit]

        content_by_id: dict[int, str] = {}
        chunk_ids = [int(entry[2]["chunk_id"]) for entry in selected]
        if chunk_ids:
            placeholders = ",".join("?" for _ in chunk_ids)
            with self.store.connect() as conn:
                content_rows = conn.execute(
                    f"SELECT id, content FROM chunks WHERE id IN ({placeholders})",
                    chunk_ids,
                ).fetchall()
            content_by_id = {
                int(row["id"]): str(row["content"] or "")
                for row in content_rows
            }

        items: list[ChunkSearchResult] = []
        for score, _ordinal, row in selected:
            chunk_id = int(row["chunk_id"])
            items.append(
                ChunkSearchResult(
                    path=str(row["path"]),
                    filename=str(row["filename"]),
                    extension=str(row["extension"]),
                    modified_time=float(row["modified_time"]),
                    size=int(row["size"]),
                    location=str(row["location"] or ""),
                    snippet=self.store._snippet_from_content(content_by_id.get(chunk_id, ""), query),
                    score=score,
                )
            )

        exact_total = len(best_by_path) if exhausted else None
        return ProgressiveSearchPage(
            items=items,
            exact_total=exact_total,
            exhausted=exhausted,
            candidates_scanned=len(rows),
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
        """Compute an exact file-level count without BM25 ranking/windowing."""
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
