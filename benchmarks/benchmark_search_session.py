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


def _stats(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index]


def _measure(callable_, *, iterations: int) -> tuple[float, float, float, list[str]]:
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
    return cold, p50, p95, [row.path for row in result.items]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compare cold-per-query vs persistent SQLite search sessions")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "session.db"
        build_synthetic_index(
            db_path,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            batch_size=32,
        )

        regular = ChunkStore(db_path)
        persistent = PersistentSearchStore(db_path)
        try:
            regular_stats = _measure(
                lambda: regular.search_page("客户经理", limit=args.limit),
                iterations=args.iterations,
            )
            persistent_stats = _measure(
                lambda: persistent.search_page("客户经理", limit=args.limit),
                iterations=args.iterations,
            )
        finally:
            persistent.close()

        reg_cold, reg_p50, reg_p95, reg_paths = regular_stats
        per_cold, per_p50, per_p95, per_paths = persistent_stats
        speedup = reg_p50 / per_p50 if per_p50 else 0.0
        print("DocSeek persistent search-session benchmark")
        print(f"files={args.files:,} chunks/file={args.chunks} page={args.limit}")
        print(
            f"new-connection: cold={reg_cold:.2f}ms p50={reg_p50:.2f}ms p95={reg_p95:.2f}ms"
        )
        print(
            f"persistent: cold={per_cold:.2f}ms p50={per_p50:.2f}ms p95={per_p95:.2f}ms"
        )
        print(
            f"warm_speedup={speedup:.2f}x exact_match={'yes' if reg_paths == per_paths else 'NO'}"
        )


if __name__ == "__main__":
    main()
