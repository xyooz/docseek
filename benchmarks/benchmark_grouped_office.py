from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_office_workload import build_office_index, workload_cases
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.query_parser import ParsedQuery, parse_query
from docseek.search_session import PersistentSearchStore


def _kwargs(parsed: ParsedQuery, *, limit: int) -> dict[str, object]:
    return {
        "limit": limit,
        "extension": parsed.extension,
        "path_contains": parsed.path_contains,
        "modified_after": parsed.modified_after,
        "modified_before": parsed.modified_before,
        "min_size": parsed.min_size,
        "max_size": parsed.max_size,
    }


def _measure(fn, *, iterations: int, warmups: int):
    started = time.perf_counter()
    page = fn()
    cold_ms = (time.perf_counter() - started) * 1000.0
    for _ in range(warmups):
        page = fn()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        page = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return cold_ms, statistics.median(samples), ordered[p95_index], page


def _signature(page) -> tuple[int, list[tuple[str, str]]]:
    return page.total_count, [(item.path, item.location) for item in page.items]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Measure production grouped Exact search on mixed office data"
    )
    parser.add_argument("--files", type=int, default=10000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--max-grouped-p95-ms",
        type=float,
        default=None,
        help="Optional hard ceiling applied to every grouped Exact workload case.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path for a machine-readable benchmark report.",
    )
    args = parser.parse_args()
    if min(
        args.files,
        args.chunks,
        args.payload_kb,
        args.batch_size,
        args.iterations,
        args.limit,
    ) < 1 or args.warmups < 0:
        parser.error("numeric size/count arguments must be >= 1; --warmups must be >= 0")
    if args.max_grouped_p95_ms is not None and args.max_grouped_p95_ms <= 0:
        parser.error("--max-grouped-p95-ms must be > 0")

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "office.db"
        index_seconds, db_bytes, source_bytes, records = build_office_index(
            db_path,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            batch_size=args.batch_size,
        )
        source_mib = source_bytes / 1024 / 1024
        database_mib = db_bytes / 1024 / 1024
        amplification = db_bytes / source_bytes if source_bytes else 0.0

        print("DocSeek production grouped Exact scale benchmark")
        print(
            f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb}KiB "
            f"page={args.limit} index={index_seconds:.3f}s "
            f"db={database_mib:.2f}MiB source={source_mib:.2f}MiB "
            f"amplification={amplification:.2f}x"
        )

        store = PersistentSearchStore(db_path)
        grouped = ExactGroupedSearchEngine(store)
        failures: list[str] = []
        latency_failures: list[str] = []
        case_reports: list[dict[str, object]] = []
        try:
            for case in workload_cases():
                parsed = parse_query(case.raw_query)
                kwargs = _kwargs(parsed, limit=args.limit)
                expected = sum(1 for record in records if case.predicate(record))

                window_cold, window_p50, window_p95, window_page = _measure(
                    lambda: store.search_page(parsed.text, **kwargs),
                    iterations=args.iterations,
                    warmups=args.warmups,
                )
                grouped_cold, grouped_p50, grouped_p95, grouped_page = _measure(
                    lambda: grouped.search_page(parsed.text, **kwargs),
                    iterations=args.iterations,
                    warmups=args.warmups,
                )

                exact = (
                    _signature(window_page) == _signature(grouped_page)
                    and window_page.total_count == expected
                )
                if not exact:
                    failures.append(case.label)

                within_latency = (
                    args.max_grouped_p95_ms is None
                    or grouped_p95 <= args.max_grouped_p95_ms
                )
                if not within_latency:
                    latency_failures.append(
                        f"{case.label}={grouped_p95:.2f}ms>{args.max_grouped_p95_ms:.2f}ms"
                    )

                speedup = window_p50 / grouped_p50 if grouped_p50 else float("inf")
                print(
                    f"- {case.label:14s} matches={grouped_page.total_count:6d} "
                    f"grouped(cold={grouped_cold:7.2f}, p50={grouped_p50:7.2f}, p95={grouped_p95:7.2f})ms "
                    f"window(p50={window_p50:7.2f}, p95={window_p95:7.2f})ms "
                    f"speedup={speedup:4.2f}x exact={'yes' if exact else 'NO'} "
                    f"latency={'ok' if within_latency else 'FAIL'}"
                )
                case_reports.append(
                    {
                        "label": case.label,
                        "query": case.raw_query,
                        "matches": grouped_page.total_count,
                        "expected_matches": expected,
                        "exact": exact,
                        "grouped": {
                            "cold_ms": grouped_cold,
                            "p50_ms": grouped_p50,
                            "p95_ms": grouped_p95,
                        },
                        "window": {
                            "cold_ms": window_cold,
                            "p50_ms": window_p50,
                            "p95_ms": window_p95,
                        },
                    }
                )
        finally:
            store.close()

        report = {
            "files": args.files,
            "chunks_per_file": args.chunks,
            "payload_kb": args.payload_kb,
            "limit": args.limit,
            "index_seconds": index_seconds,
            "files_per_second": args.files / index_seconds if index_seconds else None,
            "logical_source_mib": source_mib,
            "database_mib": database_mib,
            "index_amplification": amplification,
            "max_grouped_p95_ms": args.max_grouped_p95_ms,
            "cases": case_reports,
        }
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"json_report={args.json_out}")

        if failures:
            raise SystemExit("grouped exact semantic mismatch: " + ", ".join(failures))
        if latency_failures:
            raise SystemExit("grouped exact latency ceiling exceeded: " + "; ".join(latency_failures))


if __name__ == "__main__":
    main()
