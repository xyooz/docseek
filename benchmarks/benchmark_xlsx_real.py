from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
import time
import tracemalloc
from pathlib import Path

from openpyxl import Workbook

from docseek.chunk_store import ChunkStore
from docseek.chunks import iter_document_chunks


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
    # write_only workbook starts with a placeholder active sheet only when one
    # was created implicitly; remove it if needed.
    if "Sheet" in wb.sheetnames and len(wb.sheetnames) > sheets:
        del wb["Sheet"]
    wb.save(path)


def median_ms(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[len(ordered) // 2] * 1000.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=20_000, help="data rows per sheet")
    parser.add_argument("--sheets", type=int, default=2)
    parser.add_argument("--columns", type=int, default=10)
    parser.add_argument("--query-iterations", type=int, default=5)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="docseek-xlsx-bench-") as temp_dir:
        temp = Path(temp_dir)
        xlsx = temp / "网点客户明细.xlsx"
        db_path = temp / "docseek.db"

        generate_start = time.perf_counter()
        build_workbook(
            xlsx,
            rows=max(1, args.rows),
            sheets=max(1, args.sheets),
            columns=max(5, args.columns),
        )
        generate_seconds = time.perf_counter() - generate_start
        xlsx_size = xlsx.stat().st_size

        # Measure extraction alone first. iter_document_chunks is intentionally
        # consumed here, then opened again for the indexing measurement below.
        tracemalloc.start()
        extract_start = time.perf_counter()
        chunk_count = 0
        text_chars = 0
        for chunk in iter_document_chunks(xlsx):
            chunk_count += 1
            text_chars += len(chunk.content)
        extract_seconds = time.perf_counter() - extract_start
        _current, extract_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        store = ChunkStore(db_path)
        stat = xlsx.stat()

        tracemalloc.start()
        index_start = time.perf_counter()
        indexed_chunks = store.replace_document(
            path=str(xlsx.resolve()),
            filename=xlsx.name,
            extension=xlsx.suffix.lower(),
            modified_time=stat.st_mtime,
            size=stat.st_size,
            chunks=iter_document_chunks(xlsx),
        )
        index_seconds = time.perf_counter() - index_start
        _current, index_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        with sqlite3.connect(db_path) as conn:
            raw_bytes = int(
                conn.execute(
                    "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM chunks"
                ).fetchone()[0]
            )
        db_size = db_path.stat().st_size

        queries = ["客户经理", "身份证有效期", "客户000"]
        print("DocSeek real-XLSX benchmark")
        print(
            f"rows/sheet={args.rows:,} sheets={args.sheets} columns={args.columns} "
            f"xlsx={xlsx_size / 1024 / 1024:.2f}MiB"
        )
        print(
            f"generate={generate_seconds:.3f}s "
            f"extract={extract_seconds:.3f}s chunks={chunk_count:,} "
            f"text={text_chars / 1024 / 1024:.2f}MiChars "
            f"extract_peak_py={extract_peak / 1024 / 1024:.2f}MiB"
        )
        print(
            f"index={index_seconds:.3f}s indexed_chunks={indexed_chunks:,} "
            f"index_peak_py={index_peak / 1024 / 1024:.2f}MiB "
            f"db={db_size / 1024 / 1024:.2f}MiB raw_chunk_storage={raw_bytes / 1024 / 1024:.2f}MiB"
        )

        for query in queries:
            samples: list[float] = []
            result_count = 0
            for _ in range(max(1, args.query_iterations)):
                started = time.perf_counter()
                page = store.search_page(query, limit=100, offset=0)
                samples.append(time.perf_counter() - started)
                result_count = page.total_count
            print(
                f"query={query!r} total_files={result_count} "
                f"p50={median_ms(samples):.2f}ms"
            )

        print(
            "note=tracemalloc reports Python allocations only; openpyxl/native "
            "RSS should be measured separately on a representative office PC"
        )


if __name__ == "__main__":
    main()
