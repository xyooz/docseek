from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_search import build_synthetic_index
from docseek.chunk_store import ChunkStore
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.search_session import PersistentSearchStore


DEFAULT_QUERIES = ["客户经理", "专项稀有词"]


def _stats(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index]


def _measure(callable_, *, iterations: int) -> tuple[float, float, float, list[str], int]:
    started = time.perf_counter()
    first = callable_()
    cold = (time.perf_counter() - started) * 1000.0

    samples: list[float] = []
    result = first
    for _ in range(iterations):
        started = time.perf_counter()
        result = callable_()
        samples.append((time.perf_counter() - started) * 1000.0)
    p50, p95 = _stats(samples)
    return cold, p50, p95, [row.path for row in result.items], result.total_count


def _print_measurement(label: str, stats: tuple[float, float, float, list[str], int]) -> None:
    cold, p50, p95, _paths, _total = stats
    print(f"  {label}: cold={cold:.2f}ms p50={p50:.2f}ms p95={p95:.2f}ms")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Compare cold-per-query and persistent exact-search strategies"
    )
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "session.db"
        index_time, database_bytes, logical_source_bytes = build_synthetic_index(
            db_path,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            batch_size=32,
        )

        regular = ChunkStore(db_path)
        persistent = PersistentSearchStore(db_path)
        grouped = ExactGroupedSearchEngine(persistent)
        try:
            print("DocSeek persistent exact-search benchmark")
            print(
                f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb}KiB "
                f"page={args.limit}"
            )
            source_mib = logical_source_bytes / (1024 * 1024)
            db_mib = database_bytes / (1024 * 1024)
            amplification = database_bytes / logical_source_bytes if logical_source_bytes else 0.0
            throughput = args.files / index_time if index_time else 0.0
            print(
                f"index_time={index_time:.3f}s files_per_second={throughput:.1f} "
                f"logical_source={source_mib:.2f}MiB database_size={db_mib:.2f}MiB "
                f"index_amplification={amplification:.2f}x"
            )

            for query in args.queries:
                regular_stats = _measure(
                    lambda q=query: regular.search_page(q, limit=args.limit),
                    iterations=args.iterations,
                )
                persistent_window = _measure(
                    lambda q=query: persistent.search_page(q, limit=args.limit),
                    iterations=args.iterations,
                )
                persistent_grouped = _measure(
                    lambda q=query: grouped.search_page(q, limit=args.limit),
                    iterations=args.iterations,
                )

                reg_cold, reg_p50, _reg_p95, reg_paths, reg_total = regular_stats
                _win_cold, win_p50, _win_p95, win_paths, win_total = persistent_window
                _grp_cold, grp_p50, _grp_p95, grp_paths, grp_total = persistent_grouped

                window_exact = reg_paths == win_paths and reg_total == win_total
                grouped_exact = reg_paths == grp_paths and reg_total == grp_total
                session_speedup = reg_p50 / win_p50 if win_p50 else 0.0
                grouped_speedup = win_p50 / grp_p50 if grp_p50 else 0.0

                print(f"query={query!r} matches={reg_total:,}")
                _print_measurement("new-connection/window", regular_stats)
                _print_measurement("persistent/window", persistent_window)
                _print_measurement("persistent/grouped", persistent_grouped)
                print(
                    f"  session_speedup={session_speedup:.2f}x "
                    f"grouped_vs_window={grouped_speedup:.2f}x "
                    f"window_exact={'yes' if window_exact else 'NO'} "
                    f"grouped_exact={'yes' if grouped_exact else 'NO'}"
                )
        finally:
            persistent.close()


if __name__ == "__main__":
    main()
