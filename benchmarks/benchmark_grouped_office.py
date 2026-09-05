from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_office_workload import build_office_index, workload_cases
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.query_parser import ParsedQuery, parse_query
from docseek.search_session import PersistentSearchStore


def _kwargs(parsed: ParsedQuery, *, limit: int) -> dict[str, object]:
    return {
        "limit": limit,
        "extension": parsed.extension,
        "path_contains": parsed.path_contains,
        "modified_after": parsed.modified_after,
        "modified_before": parsed.modified_before,
        "min_size": parsed.min_size,
        "max_size": parsed.max_size,
    }


def _measure(fn, *, iterations: int, warmups: int):
    page = fn()
    for _ in range(warmups):
        page = fn()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        page = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index], page


def _signature(page) -> tuple[int, list[tuple[str, str]]]:
    return page.total_count, [(item.path, item.location) for item in page.items]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compare window Exact and grouped Exact on mixed office data")
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
        index_seconds, db_bytes, source_bytes, records = build_office_index(
            db_path,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            batch_size=args.batch_size,
        )
        print("DocSeek grouped Exact mixed-office A/B")
        print(
            f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb}KiB "
            f"index={index_seconds:.3f}s db={db_bytes / 1024 / 1024:.2f}MiB source={source_bytes / 1024 / 1024:.2f}MiB"
        )

        store = PersistentSearchStore(db_path)
        grouped = ExactGroupedSearchEngine(store)
        failures: list[str] = []
        try:
            for case in workload_cases():
                parsed = parse_query(case.raw_query)
                kwargs = _kwargs(parsed, limit=args.limit)
                expected = sum(1 for record in records if case.predicate(record))

                window_p50, window_p95, window_page = _measure(
                    lambda: store.search_page(parsed.text, **kwargs),
                    iterations=args.iterations,
                    warmups=args.warmups,
                )
                grouped_p50, grouped_p95, grouped_page = _measure(
                    lambda: grouped.search_page(parsed.text, **kwargs),
                    iterations=args.iterations,
                    warmups=args.warmups,
                )

                exact = (
                    _signature(window_page) == _signature(grouped_page)
                    and window_page.total_count == expected
                )
                if not exact:
                    failures.append(case.label)

                speedup = window_p50 / grouped_p50 if grouped_p50 else float("inf")
                print(
                    f"- {case.label:14s} matches={window_page.total_count:6d} "
                    f"window(p50={window_p50:7.2f}, p95={window_p95:7.2f})ms "
                    f"grouped(p50={grouped_p50:7.2f}, p95={grouped_p95:7.2f})ms "
                    f"speedup={speedup:4.2f}x exact={'yes' if exact else 'NO'}"
                )
        finally:
            store.close()

        if failures:
            raise SystemExit("grouped exact semantic mismatch: " + ", ".join(failures))


if __name__ == "__main__":
    main()
