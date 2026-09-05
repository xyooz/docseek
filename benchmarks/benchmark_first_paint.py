from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_office_workload import build_office_index, workload_cases
from docseek.query_parser import ParsedQuery, parse_query
from docseek.search_session import PersistentSearchStore


def _params(store: PersistentSearchStore, parsed: ParsedQuery, *, fetch_limit: int) -> tuple[str, dict[str, object], list[str], str]:
    table, fts_query = store._select_index(parsed.text)
    params: dict[str, object] = {
        "fts_query": fts_query,
        "raw_query": store._plain_query_text(parsed.text),
        "fetch_limit": fetch_limit,
    }
    clauses = [f"{table} MATCH :fts_query"]
    store._append_metadata_filters(
        clauses,
        params,
        extension=parsed.extension,
        path_contains=parsed.path_contains,
        modified_after=parsed.modified_after,
        modified_before=parsed.modified_before,
        min_size=parsed.min_size,
        max_size=parsed.max_size,
    )
    return table, params, clauses, f"bm25({table}, 5.0, 1.0)"


def first_page_without_total(store: PersistentSearchStore, parsed: ParsedQuery, *, limit: int):
    if not parsed.text:
        clauses: list[str] = []
        params: dict[str, object] = {"fetch_limit": limit + 1}
        store._append_metadata_filters(
            clauses,
            params,
            extension=parsed.extension,
            path_contains=parsed.path_contains,
            modified_after=parsed.modified_after,
            modified_before=parsed.modified_before,
            min_size=parsed.min_size,
            max_size=parsed.max_size,
        )
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT f.path, f.filename, '' AS location, '' AS raw_content
                FROM files f
                {where}
                ORDER BY f.modified_time DESC, LOWER(f.filename) ASC, f.path ASC
                LIMIT :fetch_limit
                """,
                params,
            ).fetchall()
        return [(str(r["path"]), str(r["location"])) for r in rows[:limit]], len(rows) > limit

    table, params, clauses, score_expr = _params(store, parsed, fetch_limit=limit + 1)
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
                chunk_id, path, filename, extension, modified_time, size,
                ordinal, location, relevance_score
            FROM ranked
            WHERE file_rank = 1
        ),
        paged AS (
            SELECT
                chunk_id, path, filename, extension, modified_time, size,
                ordinal, location, relevance_score
            FROM file_hits
            ORDER BY relevance_score ASC, modified_time DESC, path ASC
            LIMIT :fetch_limit
        )
        SELECT
            paged.path, paged.filename, paged.location,
            chunks.content AS raw_content
        FROM paged
        JOIN chunks ON chunks.id = paged.chunk_id
        ORDER BY paged.relevance_score ASC, paged.modified_time DESC, paged.path ASC
    """
    with store.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    # Include the same snippet work paid by production first-paint.
    for row in rows[:limit]:
        store._snippet_from_content(str(row["raw_content"] or ""), parsed.text)
    return [(str(r["path"]), str(r["location"] or "")) for r in rows[:limit]], len(rows) > limit


def _measure(fn, *, iterations: int, warmups: int):
    value = fn()
    for _ in range(warmups):
        value = fn()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        value = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index], value


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Measure exact first-page latency without synchronous total_count")
    parser.add_argument("--files", type=int, default=10000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "office.db"
        index_seconds, db_bytes, _source_bytes, records = build_office_index(
            db_path,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            batch_size=args.batch_size,
        )
        print("DocSeek first-paint exact-search A/B")
        print(f"files={args.files:,} index={index_seconds:.3f}s db={db_bytes / 1024 / 1024:.2f}MiB")

        store = PersistentSearchStore(db_path)
        failures: list[str] = []
        try:
            for case in workload_cases():
                parsed = parse_query(case.raw_query)
                expected = sum(1 for record in records if case.predicate(record))
                kwargs = dict(
                    limit=args.limit,
                    extension=parsed.extension,
                    path_contains=parsed.path_contains,
                    modified_after=parsed.modified_after,
                    modified_before=parsed.modified_before,
                    min_size=parsed.min_size,
                    max_size=parsed.max_size,
                )
                current_p50, current_p95, current_page = _measure(
                    lambda: store.search_page(parsed.text, **kwargs),
                    iterations=args.iterations,
                    warmups=args.warmups,
                )
                fast_p50, fast_p95, fast_value = _measure(
                    lambda: first_page_without_total(store, parsed, limit=args.limit),
                    iterations=args.iterations,
                    warmups=args.warmups,
                )
                fast_signature, has_more = fast_value
                current_signature = [(item.path, item.location) for item in current_page.items]
                expected_more = expected > args.limit
                exact = (
                    current_page.total_count == expected
                    and fast_signature == current_signature
                    and has_more == expected_more
                )
                if not exact:
                    failures.append(case.label)
                speedup = current_p50 / fast_p50 if fast_p50 else float("inf")
                print(
                    f"- {case.label:14s} matches={expected:6d} "
                    f"with-total(p50={current_p50:7.2f}, p95={current_p95:7.2f})ms "
                    f"first-page(p50={fast_p50:7.2f}, p95={fast_p95:7.2f})ms "
                    f"speedup={speedup:4.2f}x exact={'yes' if exact else 'NO'}"
                )
        finally:
            store.close()

        if failures:
            raise SystemExit("first-page semantic mismatch: " + ", ".join(failures))


if __name__ == "__main__":
    main()
