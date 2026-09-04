from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


def create_files(root: Path, count: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"document_{index:06d}.txt").write_text(
            f"客户经理 信贷 业务制度 文件 {index}\n第二行办公资料",
            encoding="utf-8",
        )


def timed_scan(indexer: DirectoryIndexer, root: Path) -> tuple[float, object]:
    started = time.perf_counter()
    stats = indexer.scan(root)
    return time.perf_counter() - started, stats


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DocSeek directory scan benchmark")
    parser.add_argument("--files", type=int, default=1000, help="number of small text files")
    args = parser.parse_args()
    if args.files < 1:
        parser.error("--files must be >= 1")

    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        root = base / "documents"
        db = SearchDatabase(base / "docseek.db")
        create_files(root, args.files)

        first_seconds, first_stats = timed_scan(DirectoryIndexer(db), root)
        second_seconds, second_stats = timed_scan(DirectoryIndexer(db), root)

        changed = root / "document_000000.txt"
        changed.write_text("客户经理 信贷 精准增量更新后的内容", encoding="utf-8")
        started = time.perf_counter()
        update_stats = DirectoryIndexer(db).update_paths([changed])
        update_ms = (time.perf_counter() - started) * 1000

        print("DocSeek directory scan benchmark")
        print(f"files={args.files:,}")
        print(
            f"first_scan={first_seconds:.3f}s "
            f"files_per_second={args.files / first_seconds:.1f} "
            f"indexed={first_stats.indexed}"
        )
        print(
            f"unchanged_scan={second_seconds:.3f}s "
            f"files_per_second={args.files / second_seconds:.1f} "
            f"unchanged={second_stats.unchanged}"
        )
        print(
            f"single_file_update={update_ms:.2f}ms "
            f"indexed={update_stats.indexed} removed={update_stats.removed}"
        )


if __name__ == "__main__":
    main()
