from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from benchmark_search import _database_bytes
from docseek.chunk_store import SearchPage
from docseek.chunk_writer import ChunkBatchWriter
from docseek.chunks import DocumentChunk
from docseek.query_parser import ParsedQuery, parse_query
from docseek.search_db import SearchDatabase
from docseek.search_session import PersistentSearchStore
from docseek.chunk_store import ChunkStore


EXTENSIONS = (".pdf", ".docx", ".xlsx", ".pptx")
DEPARTMENTS = ("信贷管理", "客户服务", "风险合规", "综合办公", "产品运营")


@dataclass(frozen=True, slots=True)
class SyntheticRecord:
    index: int
    extension: str
    department: str
    modified_time: float
    size: int
    common: bool
    medium: bool
    rare: bool
    multi: bool
    phrase: bool
    filename_only: bool


@dataclass(frozen=True, slots=True)
class WorkloadCase:
    label: str
    raw_query: str
    predicate: Callable[[SyntheticRecord], bool]


def _payload(base: str, payload_bytes: int) -> str:
    if len(base.encode("utf-8")) >= payload_bytes:
        return base
    filler = " 本地办公资料 业务说明 操作规范 风险提示 服务记录 文档归档 "
    pieces = [base]
    current = len(base.encode("utf-8"))
    filler_bytes = filler.encode("utf-8")
    while current + len(filler_bytes) <= payload_bytes:
        pieces.append(filler)
        current += len(filler_bytes)
    if current < payload_bytes:
        pieces.append("x" * (payload_bytes - current))
    return "".join(pieces)


def _record_for(index: int) -> SyntheticRecord:
    extension = EXTENSIONS[(index * 7 + index // 11) % len(EXTENSIONS)]
    department = DEPARTMENTS[(index * 3 + index // 17) % len(DEPARTMENTS)]
    base_date = datetime(2025, 1, 1)
    modified_time = (base_date + timedelta(days=index % 600)).timestamp()
    size = (64 + (index % 2048)) * 1024
    return SyntheticRecord(
        index=index,
        extension=extension,
        department=department,
        modified_time=modified_time,
        size=size,
        common=index % 4 == 0,          # 25%
        medium=index % 12 == 0,         # ~8.3%
        rare=index % 200 == 0,          # 0.5%
        multi=index % 20 == 0,          # 5%
        phrase=index % 50 == 0,         # 2%
        filename_only=index % 100 == 0, # 1%
    )


def _chunks_for(record: SyntheticRecord, chunks_per_file: int, payload_bytes: int) -> list[DocumentChunk]:
    chunks: list[DocumentChunk] = []
    for chunk_no in range(chunks_per_file):
        terms: list[str] = [f"文档编号 {record.index} 分块 {chunk_no + 1}"]
        if chunk_no == 0:
            if record.common:
                terms.append("业务流程")
            if record.multi:
                terms.extend(("客户经理", "信贷政策"))
        if chunk_no == min(1, chunks_per_file - 1):
            if record.medium:
                terms.append("身份证有效期")
            if record.phrase:
                terms.append("customer manager")
        if chunk_no == chunks_per_file - 1 and record.rare:
            terms.append("跨境专项复核")
        content = _payload(" ".join(terms), payload_bytes)
        chunks.append(DocumentChunk(chunk_no, f"块 {chunk_no + 1}", content))
    return chunks


def build_office_index(
    db_path: Path,
    *,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
    batch_size: int,
) -> tuple[float, int, int, list[SyntheticRecord]]:
    SearchDatabase(db_path)
    store = ChunkStore(db_path)
    payload_bytes = max(1, payload_kb) * 1024
    logical_source_bytes = 0
    records: list[SyntheticRecord] = []

    started = time.perf_counter()
    with ChunkBatchWriter(store, batch_size=batch_size) as writer:
        for index in range(files):
            record = _record_for(index)
            records.append(record)
            stem = f"办公资料_{index:06d}"
            if record.filename_only:
                stem = f"专项检查_{index:06d}"
            filename = f"{stem}{record.extension}"
            path = str(Path("C:/办公资料") / record.department / filename)
            chunks = _chunks_for(record, chunks_per_file, payload_bytes)
            logical_source_bytes += sum(len(chunk.content.encode("utf-8")) for chunk in chunks)
            writer.replace_document(
                path=path,
                filename=filename,
                extension=record.extension,
                modified_time=record.modified_time,
                size=record.size,
                chunks=chunks,
            )
    elapsed = time.perf_counter() - started
    return elapsed, _database_bytes(db_path), logical_source_bytes, records


def _after(date_text: str) -> float:
    return datetime.strptime(date_text, "%Y-%m-%d").timestamp()


def workload_cases() -> list[WorkloadCase]:
    after_2026 = _after("2026-01-01")
    one_mib = 1024 * 1024
    return [
        WorkloadCase("common", "业务流程", lambda r: r.common),
        WorkloadCase("medium", "身份证有效期", lambda r: r.medium),
        WorkloadCase("rare", "跨境专项复核", lambda r: r.rare),
        WorkloadCase("filename-only", "专项检查", lambda r: r.filename_only),
        WorkloadCase("multi-term", "客户经理 信贷政策", lambda r: r.multi),
        WorkloadCase("quoted-phrase", '"customer manager"', lambda r: r.phrase),
        WorkloadCase(
            "common+ext",
            "业务流程 ext:pdf",
            lambda r: r.common and r.extension == ".pdf",
        ),
        WorkloadCase(
            "common+path",
            "业务流程 path:信贷管理",
            lambda r: r.common and r.department == "信贷管理",
        ),
        WorkloadCase(
            "medium+date",
            "身份证有效期 after:2026-01-01",
            lambda r: r.medium and r.modified_time >= after_2026,
        ),
        WorkloadCase(
            "common+size",
            "业务流程 size:>1MB",
            lambda r: r.common and r.size > one_mib,
        ),
        WorkloadCase(
            "filter-only",
            "ext:xlsx path:风险合规",
            lambda r: r.extension == ".xlsx" and r.department == "风险合规",
        ),
        WorkloadCase(
            "combined",
            "客户经理 信贷政策 ext:pdf path:信贷管理 after:2026-01-01",
            lambda r: (
                r.multi
                and r.extension == ".pdf"
                and r.department == "信贷管理"
                and r.modified_time >= after_2026
            ),
        ),
    ]


def _search(store: PersistentSearchStore, parsed: ParsedQuery, limit: int) -> SearchPage:
    return store.search_page(
        parsed.text,
        limit=limit,
        extension=parsed.extension,
        path_contains=parsed.path_contains,
        modified_after=parsed.modified_after,
        modified_before=parsed.modified_before,
        min_size=parsed.min_size,
        max_size=parsed.max_size,
    )


def _latency(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index]


def measure_case(
    store: PersistentSearchStore,
    case: WorkloadCase,
    *,
    iterations: int,
    warmups: int,
    limit: int,
) -> tuple[float, float, float, int]:
    parsed = parse_query(case.raw_query)
    started = time.perf_counter()
    page = _search(store, parsed, limit)
    first_ms = (time.perf_counter() - started) * 1000.0

    for _ in range(warmups):
        page = _search(store, parsed, limit)

    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        page = _search(store, parsed, limit)
        samples.append((time.perf_counter() - started) * 1000.0)
    p50, p95 = _latency(samples)
    return first_ms, p50, p95, page.total_count


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DocSeek mixed office-document search workload")
    parser.add_argument("--files", type=int, default=10000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if min(args.files, args.chunks, args.payload_kb, args.batch_size, args.iterations, args.limit) < 1 or args.warmups < 0:
        parser.error("numeric size/count arguments must be >= 1; --warmups must be >= 0")

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "office.db"
        index_seconds, db_bytes, source_bytes, records = build_office_index(
            db_path,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            batch_size=args.batch_size,
        )

        source_mib = source_bytes / (1024 * 1024)
        database_mib = db_bytes / (1024 * 1024)
        amplification = db_bytes / source_bytes if source_bytes else 0.0
        print("DocSeek mixed office workload")
        print(
            f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb}KiB "
            f"page={args.limit}"
        )
        print(
            f"index_time={index_seconds:.3f}s files_per_second={args.files / index_seconds:.1f} "
            f"logical_source={source_mib:.2f}MiB database_size={database_mib:.2f}MiB "
            f"index_amplification={amplification:.2f}x"
        )
        print("production path: persistent SQLite connection + window Exact")

        store = PersistentSearchStore(db_path)
        failures: list[str] = []
        try:
            for case in workload_cases():
                expected = sum(1 for record in records if case.predicate(record))
                first_ms, p50, p95, actual = measure_case(
                    store,
                    case,
                    iterations=args.iterations,
                    warmups=args.warmups,
                    limit=args.limit,
                )
                exact = actual == expected
                if not exact:
                    failures.append(f"{case.label}: expected {expected}, got {actual}")
                selectivity = expected / len(records) * 100.0 if records else 0.0
                print(
                    f"- {case.label:14s} query={case.raw_query!r} "
                    f"matches={actual:,} selectivity={selectivity:5.2f}% "
                    f"first={first_ms:7.2f}ms p50={p50:7.2f}ms p95={p95:7.2f}ms "
                    f"count_check={'ok' if exact else 'FAIL'}"
                )
        finally:
            store.close()

        if failures:
            raise SystemExit("workload count mismatch: " + "; ".join(failures))


if __name__ == "__main__":
    main()
