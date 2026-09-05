from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_search import build_synthetic_index
from docseek.chunk_store import ChunkSearchResult, ChunkStore


def _stats(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index]


def _sql(strategy: str) -> str:
    if strategy == "path_join":
        from_sql = """
            FROM chunk_index_cjk2
            JOIN chunks c ON c.id = chunk_index_cjk2.rowid
            JOIN files f ON f.path = c.path
        """
        file_key = "f.path"
        extra_where = ""
    elif strategy == "id_join":
        from_sql = """
            FROM chunk_index_cjk2
            JOIN chunks c ON c.id = chunk_index_cjk2.rowid
            JOIN files f ON f.id = c.file_id
        """
        file_key = "f.id"
        extra_where = ""
    elif strategy == "id_cross":
        from_sql = """
            FROM chunk_index_cjk2
            CROSS JOIN chunks c
            CROSS JOIN files f
        """
        file_key = "f.id"
        extra_where = (
            "AND c.id = chunk_index_cjk2.rowid "
            "AND f.id = c.file_id"
        )
    else:
        raise ValueError(strategy)

    return f"""
        WITH hits AS (
            SELECT
                c.id AS chunk_id,
                {file_key} AS file_key,
                f.path,
                f.filename,
                f.extension,
                f.modified_time,
                f.size,
                c.ordinal,
                c.location,
                bm25(chunk_index_cjk2, 5.0, 1.0) AS bm25_score,
                CASE
                    WHEN LOWER(SUBSTR(f.filename, 1, LENGTH(f.filename) - LENGTH(f.extension))) = LOWER(:raw_query)
                        THEN -8.0
                    WHEN LOWER(f.filename) LIKE LOWER(:raw_query) || '%'
                        THEN -4.0
                    WHEN INSTR(LOWER(f.filename), LOWER(:raw_query)) > 0
                        THEN -2.0
                    ELSE 0.0
                END AS filename_boost
            {from_sql}
            WHERE chunk_index_cjk2 MATCH :fts_query {extra_where}
        ),
        ranked AS (
            SELECT
                *,
                bm25_score + filename_boost AS relevance_score,
                ROW_NUMBER() OVER (
                    PARTITION BY file_key
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
            LIMIT :limit
        )
        SELECT
            paged.path, paged.filename, paged.extension,
            paged.modified_time, paged.size, paged.location,
            chunks.content AS raw_content,
            paged.relevance_score AS score,
            paged.total_count
        FROM paged
        JOIN chunks ON chunks.id = paged.chunk_id
        ORDER BY paged.relevance_score ASC, paged.modified_time DESC, paged.path ASC
    """


def _materialize_results(store: ChunkStore, rows, query: str) -> list[ChunkSearchResult]:
    return [
        ChunkSearchResult(
            path=str(row["path"]),
            filename=str(row["filename"]),
            extension=str(row["extension"]),
            modified_time=float(row["modified_time"]),
            size=int(row["size"]),
            location=str(row["location"] or ""),
            snippet=store._snippet_from_content(str(row["raw_content"] or ""), query),
            score=float(row["score"]),
        )
        for row in rows
    ]


def _run(
    store: ChunkStore,
    strategy: str,
    *,
    iterations: int,
    limit: int,
) -> tuple[float, float, list[str], list[str]]:
    query = "客户经理"
    sql = _sql(strategy)
    params = {
        "fts_query": store._cjk_phrase(query),
        "raw_query": query,
        "limit": limit,
    }
    with store.connect() as conn:
        plan_rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        plan = [" | ".join(str(value) for value in row) for row in plan_rows]
        rows = conn.execute(sql, params).fetchall()
        _materialize_results(store, rows, query)
        samples: list[float] = []
        items: list[ChunkSearchResult] = []
        for _ in range(iterations):
            started = time.perf_counter()
            rows = conn.execute(sql, params).fetchall()
            items = _materialize_results(store, rows, query)
            samples.append((time.perf_counter() - started) * 1000.0)
    p50, p95 = _stats(samples)
    return p50, p95, [item.path for item in items], plan


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compare SQLite FTS exact join strategies")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "join-order.db"
        build_synthetic_index(
            db_path,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            batch_size=32,
        )
        store = ChunkStore(db_path)
        results: dict[str, list[str]] = {}
        print("DocSeek full exact join benchmark")
        for strategy in ("path_join", "id_join", "id_cross"):
            p50, p95, paths, plan = _run(
                store, strategy, iterations=args.iterations, limit=args.limit
            )
            results[strategy] = paths
            print(f"- {strategy}: p50={p50:.2f}ms p95={p95:.2f}ms")
            print("  plan:")
            for line in plan:
                print(f"    {line}")

        reference = results["path_join"]
        print(
            "exact_match: "
            f"id_join={'yes' if results['id_join'] == reference else 'NO'} "
            f"id_cross={'yes' if results['id_cross'] == reference else 'NO'}"
        )


if __name__ == "__main__":
    main()
