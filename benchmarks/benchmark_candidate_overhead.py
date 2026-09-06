from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import docseek.indexer as indexer_module
from docseek.chunk_writer import ChunkBatchWriter
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


def create_files(root: Path, count: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"document_{index:06d}.txt").write_text(
            f"客户经理 信贷 业务制度 文件 {index}\n第二行办公资料",
            encoding="utf-8",
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Profile the work around ChunkBatchWriter during first indexing"
    )
    parser.add_argument("--files", type=int, default=10_000)
    args = parser.parse_args()
    if args.files < 1:
        parser.error("--files must be >= 1")

    timings: dict[str, float] = {}
    counts: dict[str, int] = {}
    indexing_phase = False
    writer_depth = 0

    def add(name: str, elapsed: float) -> None:
        timings[name] = timings.get(name, 0.0) + elapsed
        counts[name] = counts.get(name, 0) + 1

    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        root = base / "documents"
        create_files(root, args.files)
        database = SearchDatabase(base / "docseek.db")
        indexer = DirectoryIndexer(database)

        original_stat = Path.stat
        original_revision = indexer_module.current_extraction_revision
        original_status = indexer_module.status_for_extraction_result
        original_replace = ChunkBatchWriter.replace_document
        original_candidate = indexer._index_existing_file

        def timed_stat(path: Path, *stat_args: Any, **stat_kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return original_stat(path, *stat_args, **stat_kwargs)
            finally:
                if indexing_phase and writer_depth == 0 and path.suffix.lower() == ".txt":
                    add("initial_stat", time.perf_counter() - started)

        def timed_revision(extension: str) -> int:
            started = time.perf_counter()
            try:
                return original_revision(extension)
            finally:
                if indexing_phase and writer_depth == 0:
                    add("revision_lookup", time.perf_counter() - started)

        def timed_status(extension: str, chunk_count: int) -> Any:
            started = time.perf_counter()
            try:
                return original_status(extension, chunk_count)
            finally:
                if indexing_phase and writer_depth == 0:
                    add("post_status", time.perf_counter() - started)

        def timed_replace(writer: ChunkBatchWriter, *call_args: Any, **call_kwargs: Any) -> Any:
            nonlocal writer_depth
            started = time.perf_counter()
            writer_depth += 1
            try:
                return original_replace(writer, *call_args, **call_kwargs)
            finally:
                writer_depth -= 1
                add("writer_replace", time.perf_counter() - started)

        def timed_candidate(*call_args: Any, **call_kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return original_candidate(*call_args, **call_kwargs)
            finally:
                add("candidate_total", time.perf_counter() - started)

        def candidates_ready(_count: int) -> None:
            nonlocal indexing_phase
            indexing_phase = True

        Path.stat = timed_stat  # type: ignore[method-assign]
        indexer_module.current_extraction_revision = timed_revision
        indexer_module.status_for_extraction_result = timed_status
        ChunkBatchWriter.replace_document = timed_replace  # type: ignore[method-assign]
        indexer._index_existing_file = timed_candidate  # type: ignore[method-assign]
        try:
            scan_started = time.perf_counter()
            stats = indexer.scan(root, on_candidates_ready=candidates_ready)
            scan_seconds = time.perf_counter() - scan_started
        finally:
            indexer._index_existing_file = original_candidate  # type: ignore[method-assign]
            ChunkBatchWriter.replace_document = original_replace  # type: ignore[method-assign]
            indexer_module.status_for_extraction_result = original_status
            indexer_module.current_extraction_revision = original_revision
            Path.stat = original_stat  # type: ignore[method-assign]

    candidate_total = timings.get("candidate_total", 0.0)
    writer = timings.get("writer_replace", 0.0)
    initial_stat = timings.get("initial_stat", 0.0)
    revision = timings.get("revision_lookup", 0.0)
    status = timings.get("post_status", 0.0)
    outside_writer = max(0.0, candidate_total - writer)
    measured_outside = initial_stat + revision + status
    python_residual = max(0.0, outside_writer - measured_outside)

    print("DocSeek first-index candidate overhead profile")
    print(
        f"files={args.files:,} scan={scan_seconds:.3f}s indexed={stats.indexed} "
        f"files_per_second={args.files / scan_seconds:.1f}"
    )
    print(
        f"candidate_total={candidate_total:.3f}s writer={writer:.3f}s "
        f"outside_writer={outside_writer:.3f}s"
    )
    print(
        f"initial_stat={initial_stat:.3f}s calls={counts.get('initial_stat', 0):,} "
        f"revision_lookup={revision:.3f}s calls={counts.get('revision_lookup', 0):,} "
        f"post_status={status:.3f}s calls={counts.get('post_status', 0):,} "
        f"python_residual={python_residual:.3f}s"
    )
    if args.files:
        print(
            f"outside_per_file_us={outside_writer * 1_000_000 / args.files:.2f} "
            f"stat_per_file_us={initial_stat * 1_000_000 / args.files:.2f} "
            f"residual_per_file_us={python_residual * 1_000_000 / args.files:.2f}"
        )


if __name__ == "__main__":
    main()
