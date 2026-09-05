from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_search import build_synthetic_index
from docseek.chunk_store import ChunkStore
from docseek.prefilter_search import MetadataPrefilterSearchEngine


def _stats(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index]


def _measure(callable_, *, warmups: int, iterations: int) -> tuple[float, float, object]:
    result = None
    for _ in range(warmups):
        result = callable_()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        result = callable_()
        samples.append((time.perf_counter() - started) * 1000.0)
    p50, p95 = _stats(samples)
    return p50, p95, result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DocSeek metadata-prefilter benchmark")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "prefilter.db"
        build_synthetic_index(
            db_path,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            batch_size=32,
        )
        store = ChunkStore(db_path)
        prefilter = MetadataPrefilterSearchEngine(store)

        scenarios = [
            ("extension~=50%", {"extension": ".pdf"}),
            ("recent~=10%", {"modified_after": float(args.files) * 0.90}),
            (
                "pdf+recent~=5%",
                {"extension": ".pdf", "modified_after": float(args.files) * 0.90},
            ),
        ]

        print("DocSeek metadata prefilter benchmark")
        print(f"files={args.files:,} chunks/file={args.chunks} page={args.limit}")
        for name, kwargs in scenarios:
            exact_p50, exact_p95, exact_page = _measure(
                lambda kwargs=kwargs: store.search_page(
                    "客户经理", limit=args.limit, **kwargs
                ),
                warmups=args.warmups,
                iterations=args.iterations,
            )
            pre_p50, pre_p95, pre_page = _measure(
                lambda kwargs=kwargs: prefilter.search_page(
                    "客户经理", limit=args.limit, **kwargs
                ),
                warmups=args.warmups,
                iterations=args.iterations,
            )
            exact_paths = [row.path for row in exact_page.items]
            pre_paths = [row.path for row in pre_page.items]
            same = (
                exact_paths == pre_paths
                and exact_page.total_count == pre_page.total_count
            )
            speedup = exact_p50 / pre_p50 if pre_p50 else 0.0
            print(
                f"- {name}: exact(p50={exact_p50:.2f}, p95={exact_p95:.2f}) ms; "
                f"prefilter(p50={pre_p50:.2f}, p95={pre_p95:.2f}) ms; "
                f"speedup={speedup:.2f}x; exact_match={'yes' if same else 'NO'}; "
                f"total={exact_page.total_count}"
            )


if __name__ == "__main__":
    main()
