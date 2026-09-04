from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase


DEFAULT_QUERIES = [
    "信贷",
    "客户经理",
    "业务制度",
    "身份证有效期",
    "customer manager",
]


def build_synthetic_index(db_path: Path, *, files: int, chunks_per_file: int) -> tuple[float, int]:
    SearchDatabase(db_path)
    store = ChunkStore(db_path)

    started = time.perf_counter()
    for index in range(files):
        extension = ".pdf" if index % 2 == 0 else ".docx"
        filename = f"业务制度_{index:06d}{extension}"
        path = str(Path("C:/benchmark") / filename)
        chunks = [
            DocumentChunk(
                ordinal=chunk_no,
                location=f"块 {chunk_no + 1}",
                content=(
                    f"客户经理 信贷 业务制度 身份证有效期 synthetic document {index} "
                    f"chunk {chunk_no} customer manager "
                ),
            )
            for chunk_no in range(chunks_per_file)
        ]
        store.replace_document(
            path=path,
            filename=filename,
            extension=extension,
            modified_time=float(index),
            size=4096 + index,
            chunks=chunks,
        )
    elapsed = time.perf_counter() - started
    return elapsed, db_path.stat().st_size


def benchmark_queries(
    db_path: Path,
    *,
    queries: list[str],
    iterations: int,
    limit: int,
) -> dict[str, dict[str, float]]:
    store = ChunkStore(db_path)
    results: dict[str, dict[str, float]] = {}

    for query in queries:
        samples_ms: list[float] = []
        result_count = 0
        for _ in range(iterations):
            started = time.perf_counter()
            rows = store.search(query, limit=limit)
            samples_ms.append((time.perf_counter() - started) * 1000)
            result_count = len(rows)

        ordered = sorted(samples_ms)
        p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
        results[query] = {
            "p50_ms": statistics.median(samples_ms),
            "p95_ms": ordered[p95_index],
            "min_ms": min(samples_ms),
            "max_ms": max(samples_ms),
            "returned": float(result_count),
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="DocSeek synthetic indexing/search benchmark")
    parser.add_argument("--files", type=int, default=1000, help="number of synthetic files")
    parser.add_argument("--chunks", type=int, default=3, help="chunks per file")
    parser.add_argument("--iterations", type=int, default=20, help="search repetitions per query")
    parser.add_argument("--limit", type=int, default=100, help="result page size")
    parser.add_argument("--db", type=Path, default=None, help="optional persistent benchmark database")
    args = parser.parse_args()

    if args.files < 1 or args.chunks < 1 or args.iterations < 1 or args.limit < 1:
        parser.error("--files, --chunks, --iterations and --limit must all be >= 1")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.db is None:
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "benchmark.db"
    else:
        db_path = args.db.expanduser().resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()

    index_seconds, db_bytes = build_synthetic_index(
        db_path,
        files=args.files,
        chunks_per_file=args.chunks,
    )
    query_stats = benchmark_queries(
        db_path,
        queries=DEFAULT_QUERIES,
        iterations=args.iterations,
        limit=args.limit,
    )

    print("DocSeek benchmark")
    print(f"files={args.files:,} chunks/file={args.chunks} page={args.limit}")
    print(f"index_time={index_seconds:.3f}s files_per_second={args.files / index_seconds:.1f}")
    print(f"database_size={db_bytes / (1024 * 1024):.2f} MiB")
    print()
    print("query latency")
    for query, stats in query_stats.items():
        print(
            f"- {query!r}: p50={stats['p50_ms']:.2f} ms "
            f"p95={stats['p95_ms']:.2f} ms "
            f"min={stats['min_ms']:.2f} ms max={stats['max_ms']:.2f} ms "
            f"returned={int(stats['returned'])}"
        )

    if temp_dir is not None:
        temp_dir.cleanup()


if __name__ == "__main__":
    main()
