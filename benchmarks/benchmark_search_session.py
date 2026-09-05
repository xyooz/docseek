from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_search import build_synthetic_index
from docseek.chunk_store import ChunkStore
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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Compare cold-per-query vs persistent SQLite search sessions"
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
        try:
            print("DocSeek persistent search-session benchmark")
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
                persistent_stats = _measure(
                    lambda q=query: persistent.search_page(q, limit=args.limit),
                    iterations=args.iterations,
                )

                reg_cold, reg_p50, reg_p95, reg_paths, reg_total = regular_stats
                per_cold, per_p50, per_p95, per_paths, per_total = persistent_stats
                speedup = reg_p50 / per_p50 if per_p50 else 0.0
                exact = reg_paths == per_paths and reg_total == per_total
                print(f"query={query!r} matches={reg_total:,}")
                print(
                    f"  new-connection: cold={reg_cold:.2f}ms p50={reg_p50:.2f}ms "
                    f"p95={reg_p95:.2f}ms"
                )
                print(
                    f"  persistent: cold={per_cold:.2f}ms p50={per_p50:.2f}ms "
                    f"p95={per_p95:.2f}ms"
                )
                print(
                    f"  warm_speedup={speedup:.2f}x exact_match={'yes' if exact else 'NO'}"
                )
        finally:
            persistent.close()


if __name__ == "__main__":
    main()
