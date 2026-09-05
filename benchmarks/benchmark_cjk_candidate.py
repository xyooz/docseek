from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from docseek.chunk_codec import decode_chunk_content, encode_chunk_content
from docseek.chunk_store import ChunkStore


QUERIES = ["信贷", "客户经理", "身份证有效期", "跨境专项复核"]


def _database_bytes(path: Path) -> int:
    total = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            total += candidate.stat().st_size
    return total


def _payload(index: int, chunk_no: int, payload_bytes: int) -> str:
    parts = [f"文档 {index} 分块 {chunk_no} 银行办公资料 业务流程 风险管理 客户服务"]
    if index % 2 == 0 and chunk_no == 0:
        parts.append("信贷")
    if index % 4 == 0 and chunk_no == 0:
        parts.append("客户经理")
    elif index % 9 == 0 and chunk_no == 0:
        # Deliberately contains the component bigrams without the exact phrase.
        parts.append("客户服务 户经理解 经理培训")
    if index % 12 == 0 and chunk_no == 1:
        parts.append("身份证有效期")
    elif index % 31 == 0 and chunk_no == 1:
        parts.append("身份证核验 证有效记录 有效期管理")
    if index % 200 == 0 and chunk_no == 2:
        parts.append("跨境专项复核")
    elif index % 401 == 0 and chunk_no == 2:
        parts.append("跨境业务 境专项目 专项复查 项复核验")

    text = " ".join(parts)
    filler = " 农业银行 本地资料 操作规范 客户服务 风险提示 业务办理 信贷政策 客户经理服务 "
    while len(text.encode("utf-8")) + len(filler.encode("utf-8")) <= payload_bytes:
        text += filler
    remaining = payload_bytes - len(text.encode("utf-8"))
    if remaining > 0:
        text += "x" * remaining
    return text


def _all_bigrams(text: str) -> str:
    return ChunkStore._cjk_bigrams(text)


def _unique_bigrams(text: str) -> str:
    # Preserve first-seen order only for deterministic output. Search uses AND,
    # so positional information is intentionally discarded.
    return " ".join(dict.fromkeys(_all_bigrams(text).split()))


def _query_bigrams(query: str) -> list[str]:
    return _all_bigrams(query).split()


def _fts_phrase(query: str) -> str:
    tokens = _query_bigrams(query)
    escaped = " ".join(token.replace('"', '""') for token in tokens)
    return f'"{escaped}"'


def _fts_and(query: str) -> str:
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in _query_bigrams(query))


def _create_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.executescript(
        """
        CREATE TABLE chunks(
            id INTEGER PRIMARY KEY,
            content BLOB NOT NULL
        );
        CREATE VIRTUAL TABLE cjk_index USING fts5(
            tokens,
            content='',
            tokenize='unicode61'
        );
        """
    )
    return conn


def _build(
    path: Path,
    *,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
    compact: bool,
) -> tuple[float, int, int]:
    payload_bytes = payload_kb * 1024
    conn = _create_database(path)
    logical_bytes = 0
    token_bytes = 0
    started = time.perf_counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rowid = 0
        for index in range(files):
            for chunk_no in range(chunks_per_file):
                rowid += 1
                content = _payload(index, chunk_no, payload_bytes)
                logical_bytes += len(content.encode("utf-8"))
                tokens = _unique_bigrams(content) if compact else _all_bigrams(content)
                token_bytes += len(tokens.encode("utf-8"))
                conn.execute(
                    "INSERT INTO chunks(id, content) VALUES (?, ?)",
                    (rowid, encode_chunk_content(content)),
                )
                conn.execute(
                    "INSERT INTO cjk_index(rowid, tokens) VALUES (?, ?)",
                    (rowid, tokens),
                )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        elapsed = time.perf_counter() - started
    finally:
        conn.close()
    return elapsed, logical_bytes, token_bytes


def _baseline_search(conn: sqlite3.Connection, query: str) -> list[int]:
    return [
        int(row[0])
        for row in conn.execute(
            "SELECT rowid FROM cjk_index WHERE cjk_index MATCH ? ORDER BY rowid",
            (_fts_phrase(query),),
        )
    ]


def _candidate_search(conn: sqlite3.Connection, query: str) -> tuple[list[int], int]:
    candidate_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT rowid FROM cjk_index WHERE cjk_index MATCH ? ORDER BY rowid",
            (_fts_and(query),),
        )
    ]
    exact: list[int] = []
    for rowid in candidate_ids:
        row = conn.execute("SELECT content FROM chunks WHERE id = ?", (rowid,)).fetchone()
        if row is not None and query in decode_chunk_content(row[0]):
            exact.append(rowid)
    return exact, len(candidate_ids)


def _measure(callable_, iterations: int) -> tuple[float, float, object]:
    samples: list[float] = []
    result: object = None
    for _ in range(iterations):
        started = time.perf_counter()
        result = callable_()
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index], result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compare positional and compact CJK indexes")
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if min(args.files, args.chunks, args.payload_kb, args.iterations) < 1:
        parser.error("all numeric arguments must be >= 1")

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_db = root / "baseline.db"
        candidate_db = root / "candidate.db"

        base_time, logical_bytes, base_token_bytes = _build(
            baseline_db,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            compact=False,
        )
        compact_time, compact_logical_bytes, compact_token_bytes = _build(
            candidate_db,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            compact=True,
        )
        assert compact_logical_bytes == logical_bytes

        base_size = _database_bytes(baseline_db)
        compact_size = _database_bytes(candidate_db)
        print("DocSeek compact CJK candidate-index experiment")
        print(
            f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb}KiB "
            f"logical={logical_bytes / 1024 / 1024:.2f}MiB"
        )
        print(
            f"baseline: build={base_time:.3f}s db={base_size / 1024 / 1024:.2f}MiB "
            f"token_text={base_token_bytes / 1024 / 1024:.2f}MiB"
        )
        print(
            f"compact : build={compact_time:.3f}s db={compact_size / 1024 / 1024:.2f}MiB "
            f"token_text={compact_token_bytes / 1024 / 1024:.2f}MiB "
            f"build_speedup={base_time / compact_time:.2f}x "
            f"db_ratio={compact_size / base_size:.3f}x token_ratio={compact_token_bytes / base_token_bytes:.3f}x"
        )

        base_conn = sqlite3.connect(baseline_db)
        compact_conn = sqlite3.connect(candidate_db)
        try:
            for query in QUERIES:
                base_p50, base_p95, base_result = _measure(
                    lambda q=query: _baseline_search(base_conn, q), args.iterations
                )
                compact_p50, compact_p95, compact_result = _measure(
                    lambda q=query: _candidate_search(compact_conn, q), args.iterations
                )
                base_ids = list(base_result)
                compact_ids, candidate_count = compact_result
                exact = base_ids == compact_ids
                amplification = candidate_count / len(base_ids) if base_ids else float(candidate_count)
                print(
                    f"- {query!r}: exact={'yes' if exact else 'NO'} hits={len(base_ids):,} "
                    f"candidates={candidate_count:,} amplification={amplification:.2f}x; "
                    f"baseline(p50={base_p50:.2f}, p95={base_p95:.2f})ms; "
                    f"compact+verify(p50={compact_p50:.2f}, p95={compact_p95:.2f})ms"
                )
        finally:
            base_conn.close()
            compact_conn.close()


if __name__ == "__main__":
    main()
