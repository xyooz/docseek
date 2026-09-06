from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_search import _database_bytes, _payload_text
from docseek.chunk_store import ChunkStore
from docseek.chunk_writer import ChunkBatchWriter
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase


PRODUCTION_MAX_BATCH_TEXT_CHARS = 8_000_000


def build_text_lane_index(
    db_path: Path,
    *,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
    batch_size: int,
) -> tuple[float, int, int]:
    """Exercise the same batched lane used by real small text documents.

    The previous benchmark labelled synthetic documents as PDF/DOCX. Production
    deliberately flushes those formats after every spool-backed document, so
    changing ``batch_size`` could not affect that benchmark at all. Using .txt
    here keeps the writer transaction open exactly like a real cheap full-scan
    batch while retaining the production 8M extracted-text safety bound.
    """
    SearchDatabase(db_path)
    store = ChunkStore(db_path)
    payload_bytes = max(1, payload_kb) * 1024
    logical_bytes = 0

    started = time.perf_counter()
    with ChunkBatchWriter(
        store,
        batch_size=batch_size,
        max_batch_text_chars=PRODUCTION_MAX_BATCH_TEXT_CHARS,
    ) as writer:
        for index in range(files):
            filename = f"业务制度_{index:06d}.txt"
            path = str(Path("C:/benchmark") / filename)
            chunks: list[DocumentChunk] = []
            for chunk_no in range(chunks_per_file):
                content = _payload_text(index, chunk_no, payload_bytes)
                logical_bytes += len(content.encode("utf-8"))
                chunks.append(DocumentChunk(chunk_no, f"行块 {chunk_no + 1}", content))

            writer.replace_document(
                path=path,
                filename=filename,
                extension=".txt",
                modified_time=float(index),
                size=sum(len(chunk.content.encode("utf-8")) for chunk in chunks),
                chunks=chunks,
            )

    elapsed = time.perf_counter() - started
    with store.connect() as conn:
        indexed = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    if indexed != files:
        raise RuntimeError(f"batch benchmark indexed {indexed} files, expected {files}")
    return elapsed, _database_bytes(db_path), logical_bytes


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Compare DocSeek full-index transaction batch sizes on the real text lane"
    )
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument(
        "--batch-sizes",
        type=int,
        nargs="+",
        default=[64, 128, 256, 512, 1024],
    )
    args = parser.parse_args()

    files = max(1, args.files)
    chunks = max(1, args.chunks)
    iterations = max(1, args.iterations)
    batch_sizes = [max(1, int(value)) for value in args.batch_sizes]

    results: list[tuple[int, float, float, int, int]] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        for batch_size in batch_sizes:
            samples: list[float] = []
            db_bytes = 0
            logical_bytes = 0
            for iteration in range(iterations):
                db_path = root / f"batch-{batch_size}-{iteration}.db"
                elapsed, db_bytes, logical_bytes = build_text_lane_index(
                    db_path,
                    files=files,
                    chunks_per_file=chunks,
                    payload_kb=args.payload_kb,
                    batch_size=batch_size,
                )
                samples.append(elapsed)
            results.append(
                (
                    batch_size,
                    statistics.median(samples),
                    max(samples),
                    db_bytes,
                    logical_bytes,
                )
            )

    fastest = min(results, key=lambda item: item[1])
    baseline = next((item for item in results if item[0] == 128), results[0])

    print("DocSeek indexing batch-size sweep — production small-text lane")
    print(
        f"files={files:,} chunks/file={chunks} payload/chunk~={args.payload_kb}KiB "
        f"iterations={iterations} max_text_chars={PRODUCTION_MAX_BATCH_TEXT_CHARS:,}"
    )
    for batch_size, median_s, max_s, db_bytes, logical_bytes in results:
        print(
            f"- batch={batch_size:4d}: median={median_s:7.3f}s max={max_s:7.3f}s "
            f"files/s={files / median_s:7.1f} "
            f"db={db_bytes / 1024 / 1024:7.2f}MiB "
            f"amplification={db_bytes / logical_bytes if logical_bytes else 0.0:.3f}x "
            f"vs128={baseline[1] / median_s:4.2f}x"
        )
    print(
        f"fastest=batch {fastest[0]} ({fastest[1]:.3f}s median, "
        f"{files / fastest[1]:.1f} files/s)"
    )


if __name__ == "__main__":
    main()
