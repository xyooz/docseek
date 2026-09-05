from __future__ import annotations

import argparse
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from openpyxl import Workbook

from docseek.chunk_store import ChunkStore
from docseek.chunks import iter_document_chunks
from docseek.search_db import SearchDatabase


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def build_workbook(path: Path, *, rows: int, sheets: int, columns: int) -> None:
    wb = Workbook(write_only=True)
    for sheet_index in range(sheets):
        ws = wb.create_sheet(title=f"客户明细{sheet_index + 1}")
        ws.append([f"字段{i + 1}" for i in range(columns)])
        for row_index in range(1, rows + 1):
            row: list[object] = []
            for col in range(columns):
                if col == 0:
                    value: object = f"客户{sheet_index:02d}-{row_index:07d}"
                elif col == 1:
                    value = "客户经理" if row_index % 7 == 0 else "普通客户"
                elif col == 2:
                    value = "身份证有效期" if row_index % 113 == 0 else "资料完整"
                elif col == 3:
                    value = row_index * 100 + sheet_index
                elif col == 4 and row_index % 5 == 0:
                    value = None
                else:
                    value = f"业务字段{col}-{row_index % 97}"
                row.append(value)
            ws.append(row)
    wb.save(path)


def peak_rss_bytes() -> int:
    if os.name == "nt":
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            process,
            ctypes.byref(counters),
            counters.cb,
        )
        return int(counters.PeakWorkingSetSize) if ok else 0

    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, AttributeError):
        return 0


def database_bytes(db_path: Path) -> int:
    return sum(
        path.stat().st_size
        for path in (
            db_path,
            Path(str(db_path) + "-wal"),
            Path(str(db_path) + "-shm"),
        )
        if path.exists()
    )


def median_ms(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[len(ordered) // 2] * 1000.0


def extract_stage(xlsx: Path) -> dict[str, object]:
    started = time.perf_counter()
    chunk_count = 0
    text_chars = 0
    for chunk in iter_document_chunks(xlsx):
        chunk_count += 1
        text_chars += len(chunk.content)
    return {
        "stage": "extract",
        "seconds": time.perf_counter() - started,
        "chunks": chunk_count,
        "text_chars": text_chars,
        "peak_rss": peak_rss_bytes(),
    }


def index_stage(xlsx: Path, db_path: Path, query_iterations: int) -> dict[str, object]:
    # Match the production startup order: SearchDatabase owns the shared
    # metadata/settings tables, then ChunkStore upgrades/opens the chunk schema.
    SearchDatabase(db_path)
    store = ChunkStore(db_path)
    stat = xlsx.stat()

    started = time.perf_counter()
    indexed_chunks = store.replace_document(
        path=str(xlsx.resolve()),
        filename=xlsx.name,
        extension=xlsx.suffix.lower(),
        modified_time=stat.st_mtime,
        size=stat.st_size,
        chunks=iter_document_chunks(xlsx),
    )
    index_seconds = time.perf_counter() - started

    with sqlite3.connect(db_path) as conn:
        raw_bytes = int(
            conn.execute(
                "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM chunks"
            ).fetchone()[0]
        )

    queries: dict[str, dict[str, object]] = {}
    for query in ("客户经理", "身份证有效期", "客户00-0000113"):
        samples: list[float] = []
        total_count = 0
        for _ in range(max(1, query_iterations)):
            query_started = time.perf_counter()
            page = store.search_page(query, limit=100, offset=0)
            samples.append(time.perf_counter() - query_started)
            total_count = page.total_count
        queries[query] = {
            "p50_ms": median_ms(samples),
            "total_files": total_count,
        }

    return {
        "stage": "index",
        "seconds": index_seconds,
        "indexed_chunks": indexed_chunks,
        "raw_chunk_bytes": raw_bytes,
        "database_bytes": database_bytes(db_path),
        "peak_rss": peak_rss_bytes(),
        "queries": queries,
    }


def run_child(*args: str) -> dict[str, object]:
    command = [sys.executable, str(Path(__file__).resolve()), *args]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    output = subprocess.check_output(command, text=True, encoding="utf-8", env=env)
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"benchmark child produced no output: {command}")
    return json.loads(lines[-1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=20_000, help="data rows per sheet")
    parser.add_argument("--sheets", type=int, default=2)
    parser.add_argument("--columns", type=int, default=10)
    parser.add_argument("--query-iterations", type=int, default=5)
    parser.add_argument("--stage", choices=("all", "extract", "index"), default="all")
    parser.add_argument("--xlsx", type=Path)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()

    if args.stage == "extract":
        if args.xlsx is None:
            parser.error("--xlsx is required for extract stage")
        print(json.dumps(extract_stage(args.xlsx), ensure_ascii=False))
        return

    if args.stage == "index":
        if args.xlsx is None or args.db is None:
            parser.error("--xlsx and --db are required for index stage")
        print(
            json.dumps(
                index_stage(args.xlsx, args.db, args.query_iterations),
                ensure_ascii=False,
            )
        )
        return

    with tempfile.TemporaryDirectory(prefix="docseek-xlsx-bench-") as temp_dir:
        temp = Path(temp_dir)
        xlsx = temp / "网点客户明细.xlsx"
        db_path = temp / "docseek.db"

        generate_started = time.perf_counter()
        build_workbook(
            xlsx,
            rows=max(1, args.rows),
            sheets=max(1, args.sheets),
            columns=max(5, args.columns),
        )
        generate_seconds = time.perf_counter() - generate_started

        extract = run_child("--stage", "extract", "--xlsx", str(xlsx))
        index = run_child(
            "--stage",
            "index",
            "--xlsx",
            str(xlsx),
            "--db",
            str(db_path),
            "--query-iterations",
            str(args.query_iterations),
        )

        print("DocSeek real-XLSX benchmark")
        print(
            f"rows/sheet={args.rows:,} sheets={args.sheets} columns={args.columns} "
            f"xlsx={xlsx.stat().st_size / 1024 / 1024:.2f}MiB"
        )
        print(f"generate={generate_seconds:.3f}s")
        print(
            f"extract={float(extract['seconds']):.3f}s "
            f"chunks={int(extract['chunks']):,} "
            f"text={int(extract['text_chars']) / 1024 / 1024:.2f}MiChars "
            f"peak_rss={int(extract['peak_rss']) / 1024 / 1024:.2f}MiB"
        )
        print(
            f"index={float(index['seconds']):.3f}s "
            f"indexed_chunks={int(index['indexed_chunks']):,} "
            f"peak_rss={int(index['peak_rss']) / 1024 / 1024:.2f}MiB "
            f"db+wal={int(index['database_bytes']) / 1024 / 1024:.2f}MiB "
            f"compressed_raw={int(index['raw_chunk_bytes']) / 1024 / 1024:.2f}MiB"
        )
        for query, metrics in dict(index["queries"]).items():
            print(
                f"query={query!r} total_files={int(metrics['total_files'])} "
                f"p50={float(metrics['p50_ms']):.2f}ms"
            )


if __name__ == "__main__":
    main()
