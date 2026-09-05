from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from benchmark_search import build_synthetic_index


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compare DocSeek full-index transaction batch sizes")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument(
        "--batch-sizes",
        type=int,
        nargs="+",
        default=[16, 32, 64, 128, 256],
    )
    args = parser.parse_args()

    results: list[tuple[int, float, int, int]] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        for batch_size in args.batch_sizes:
            db_path = root / f"batch-{batch_size}.db"
            elapsed, db_bytes, logical_bytes = build_synthetic_index(
                db_path,
                files=args.files,
                chunks_per_file=args.chunks,
                payload_kb=args.payload_kb,
                batch_size=batch_size,
            )
            results.append((batch_size, elapsed, db_bytes, logical_bytes))

    fastest = min(results, key=lambda item: item[1])
    baseline = next((item for item in results if item[0] == 32), results[0])

    print("DocSeek indexing batch-size sweep")
    print(
        f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb}KiB"
    )
    for batch_size, elapsed, db_bytes, logical_bytes in results:
        print(
            f"- batch={batch_size:3d}: build={elapsed:7.3f}s "
            f"files/s={args.files / elapsed:7.1f} "
            f"db={db_bytes / 1024 / 1024:7.2f}MiB "
            f"amplification={db_bytes / logical_bytes if logical_bytes else 0.0:.3f}x "
            f"vs32={baseline[1] / elapsed:4.2f}x"
        )
    print(
        f"fastest=batch {fastest[0]} ({fastest[1]:.3f}s, "
        f"{args.files / fastest[1]:.1f} files/s)"
    )


if __name__ == "__main__":
    main()
