from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunk_writer import ChunkBatchWriter
from docseek.chunks import DocumentChunk
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.progressive_search import ProgressiveSearchEngine
from docseek.search_db import SearchDatabase


DEFAULT_QUERIES = [
    "信贷",
    "客户经理",
    "身份证有效期",
    "customer manager",
    "专项稀有词",
]


def _database_bytes(db_path: Path) -> int:
    total = 0
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            total += candidate.stat().st_size
    return total


def _payload_text(index: int, chunk_no: int, payload_bytes: int) -> str:
    base = (
        f"客户经理 信贷 业务制度 身份证有效期 synthetic document {index} "
        f"chunk {chunk_no} customer manager "
    )
    if index % 100 == 0 and chunk_no == 0:
        base += "专项稀有词 "
    if payload_bytes <= len(base.encode("utf-8")):
        return base

    filler_unit = "银行办公资料流程说明 风险管理 客户服务 业务操作 local search payload "
    pieces = [base]
    current = len(base.encode("utf-8"))
    filler_bytes = filler_unit.encode("utf-8")
    while current + len(filler_bytes) <= payload_bytes:
        pieces.append(filler_unit)
        current += len(filler_bytes)
    if current < payload_bytes:
        pieces.append("x" * (payload_bytes - current))
    return "".join(pieces)


def build_synthetic_index(
    db_path: Path,
    *,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
    batch_size: int,
) -> tuple[float, int, int]:
    SearchDatabase(db_path)
    store = ChunkStore(db_path)
    payload_bytes = max(1, payload_kb) * 1024
    logical_source_bytes = 0

    started = time.perf_counter()
    with ChunkBatchWriter(store, batch_size=batch_size) as writer:
        for index in range(files):
            extension = ".pdf" if index % 2 == 0 else ".docx"
            filename = f"业务制度_{index:06d}{extension}"
            path = str(Path("C:/benchmark") / filename)
            chunks: list[DocumentChunk] = []
            for chunk_no in range(chunks_per_file):
                content = _payload_text(index, chunk_no, payload_bytes)
                logical_source_bytes += len(content.encode("utf-8"))
                chunks.append(DocumentChunk(chunk_no, f"块 {chunk_no + 1}", content))
            writer.replace_document(
                path=path,
                filename=filename,
                extension=extension,
                modified_time=float(index),
                size=sum(len(chunk.content.encode("utf-8")) for chunk in chunks),
                chunks=chunks,
            )
    elapsed = time.perf_counter() - started
    return elapsed, _database_bytes(db_path), logical_source_bytes


def _latency_stats(samples_ms: list[float]) -> tuple[float, float]:
    ordered = sorted(samples_ms)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples_ms), ordered[p95_index]


def _recall(reference_paths: list[str], candidate_paths: list[str]) -> float:
    if not reference_paths:
        return 1.0 if not candidate_paths else 0.0
    reference = set(reference_paths)
    return len(reference.intersection(candidate_paths)) / len(reference)


def benchmark_queries(
    db_path: Path,
    *,
    queries: list[str],
    iterations: int,
    warmups: int,
    limit: int,
    candidate_multiplier: int,
) -> dict[str, dict[str, float]]:
    store = ChunkStore(db_path)
    grouped = ExactGroupedSearchEngine(store)
    progressive = ProgressiveSearchEngine(store)
    results: dict[str, dict[str, float]] = {}

    for query in queries:
        started = time.perf_counter()
        window_page = store.search_page(query, limit=limit)
        window_cold_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        grouped_page = grouped.search_page(query, limit=limit)
        grouped_cold_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        quick_page = progressive.search_topk(
            query,
            limit=limit,
            candidate_multiplier=candidate_multiplier,
        )
        progressive_cold_ms = (time.perf_counter() - started) * 1000

        for _ in range(warmups):
            store.search_page(query, limit=limit)
            grouped.search_page(query, limit=limit)
            progressive.search_topk(
                query,
                limit=limit,
                candidate_multiplier=candidate_multiplier,
            )

        window_samples: list[float] = []
        grouped_samples: list[float] = []
        quick_samples: list[float] = []
        count_samples: list[float] = []
        exact_count = 0
        for _ in range(iterations):
            started = time.perf_counter()
            window_page = store.search_page(query, limit=limit)
            window_samples.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            grouped_page = grouped.search_page(query, limit=limit)
            grouped_samples.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            quick_page = progressive.search_topk(
                query,
                limit=limit,
                candidate_multiplier=candidate_multiplier,
            )
            quick_samples.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            exact_count = grouped.count_files(query)
            count_samples.append((time.perf_counter() - started) * 1000)

        window_p50, window_p95 = _latency_stats(window_samples)
        grouped_p50, grouped_p95 = _latency_stats(grouped_samples)
        quick_p50, quick_p95 = _latency_stats(quick_samples)
        count_p50, count_p95 = _latency_stats(count_samples)
        window_paths = [row.path for row in window_page.items]
        grouped_paths = [row.path for row in grouped_page.items]
        quick_paths = [row.path for row in quick_page.items]
        exact_match = float(
            window_paths == grouped_paths
            and window_page.total_count == grouped_page.total_count
        )
        results[query] = {
            "window_cold_ms": window_cold_ms,
            "window_p50_ms": window_p50,
            "window_p95_ms": window_p95,
            "grouped_cold_ms": grouped_cold_ms,
            "grouped_p50_ms": grouped_p50,
            "grouped_p95_ms": grouped_p95,
            "quick_cold_ms": progressive_cold_ms,
            "quick_p50_ms": quick_p50,
            "quick_p95_ms": quick_p95,
            "count_p50_ms": count_p50,
            "count_p95_ms": count_p95,
            "returned": float(len(quick_page.items)),
            "total_count": float(exact_count),
            "candidates": float(quick_page.candidates_scanned),
            "progressive_recall": _recall(window_paths, quick_paths),
            "grouped_exact_match": exact_match,
        }
    return results


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DocSeek synthetic indexing/search benchmark")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--candidate-multiplier", type=int, default=8)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()

    if min(args.files, args.chunks, args.payload_kb, args.batch_size, args.iterations, args.limit, args.candidate_multiplier) < 1 or args.warmups < 0:
        parser.error("numeric size/count arguments must be >= 1; --warmups must be >= 0")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.db is None:
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "benchmark.db"
    else:
        db_path = args.db.expanduser().resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if candidate.exists():
                candidate.unlink()

    index_seconds, db_bytes, source_bytes = build_synthetic_index(
        db_path,
        files=args.files,
        chunks_per_file=args.chunks,
        payload_kb=args.payload_kb,
        batch_size=args.batch_size,
    )
    query_stats = benchmark_queries(
        db_path,
        queries=DEFAULT_QUERIES,
        iterations=args.iterations,
        warmups=args.warmups,
        limit=args.limit,
        candidate_multiplier=args.candidate_multiplier,
    )

    source_mib = source_bytes / (1024 * 1024)
    database_mib = db_bytes / (1024 * 1024)
    throughput_mib_s = source_mib / index_seconds
    amplification = db_bytes / source_bytes if source_bytes else 0.0

    print("DocSeek benchmark")
    print(
        f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb} KiB "
        f"batch={args.batch_size} page={args.limit} candidate_multiplier={args.candidate_multiplier}"
    )
    print(
        f"index_time={index_seconds:.3f}s files_per_second={args.files / index_seconds:.1f} "
        f"source_throughput={throughput_mib_s:.2f} MiB/s"
    )
    print(
        f"logical_source={source_mib:.2f} MiB database_size={database_mib:.2f} MiB "
        f"index_amplification={amplification:.2f}x"
    )
    print()
    print("query latency: exact-window vs exact-grouped vs progressive top-k")
    for query, stats in query_stats.items():
        print(
            f"- {query!r}: "
            f"window(cold={stats['window_cold_ms']:.2f}, p50={stats['window_p50_ms']:.2f}, p95={stats['window_p95_ms']:.2f}) ms; "
            f"grouped(cold={stats['grouped_cold_ms']:.2f}, p50={stats['grouped_p50_ms']:.2f}, p95={stats['grouped_p95_ms']:.2f}) ms; "
            f"topk(cold={stats['quick_cold_ms']:.2f}, p50={stats['quick_p50_ms']:.2f}, p95={stats['quick_p95_ms']:.2f}) ms; "
            f"count(p50={stats['count_p50_ms']:.2f}, p95={stats['count_p95_ms']:.2f}) ms; "
            f"exact_match={'yes' if stats['grouped_exact_match'] else 'NO'}; "
            f"topk_recall@{args.limit}={stats['progressive_recall'] * 100:.1f}%; "
            f"returned={int(stats['returned'])}/{int(stats['total_count'])} candidates={int(stats['candidates'])}"
        )

    if temp_dir is not None:
        temp_dir.cleanup()


if __name__ == "__main__":
    main()
