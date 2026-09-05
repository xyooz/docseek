from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from docseek.chunk_codec import encode_chunk_content
from docseek.chunk_store import ChunkStore

from benchmark_cjk_candidate import QUERIES, _database_bytes, _payload


def _all_bigrams(text: str) -> str:
    return ChunkStore._cjk_bigrams(text)


def _unique_bigrams(text: str) -> str:
    return " ".join(dict.fromkeys(_all_bigrams(text).split()))


def _baseline_query(query: str) -> str:
    tokens = _all_bigrams(query).split()
    escaped = " ".join(token.replace('"', '""') for token in tokens)
    return f'"{escaped}"'


def _build_baseline(
    path: Path,
    *,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
) -> tuple[float, int]:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, content BLOB NOT NULL);
        CREATE VIRTUAL TABLE cjk_index USING fts5(
            tokens,
            content='',
            tokenize='unicode61'
        );
        """
    )
    token_bytes = 0
    started = time.perf_counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rowid = 0
        for index in range(files):
            for chunk_no in range(chunks_per_file):
                rowid += 1
                content = _payload(index, chunk_no, payload_kb * 1024)
                tokens = _all_bigrams(content)
                token_bytes += len(tokens.encode("utf-8"))
                conn.execute("INSERT INTO chunks(id, content) VALUES (?, ?)", (rowid, encode_chunk_content(content)))
                conn.execute("INSERT INTO cjk_index(rowid, tokens) VALUES (?, ?)", (rowid, tokens))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        elapsed = time.perf_counter() - started
    finally:
        conn.close()
    return elapsed, token_bytes


def _build_hybrid(
    path: Path,
    *,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
) -> tuple[float, int]:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, content BLOB NOT NULL);
        CREATE VIRTUAL TABLE trigram_index USING fts5(
            content,
            content='',
            tokenize='trigram case_sensitive 0'
        );
        CREATE VIRTUAL TABLE short_index USING fts5(
            tokens,
            content='',
            tokenize='unicode61'
        );
        """
    )
    short_token_bytes = 0
    started = time.perf_counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rowid = 0
        for index in range(files):
            for chunk_no in range(chunks_per_file):
                rowid += 1
                content = _payload(index, chunk_no, payload_kb * 1024)
                short_tokens = _unique_bigrams(content)
                short_token_bytes += len(short_tokens.encode("utf-8"))
                conn.execute("INSERT INTO chunks(id, content) VALUES (?, ?)", (rowid, encode_chunk_content(content)))
                conn.execute("INSERT INTO trigram_index(rowid, content) VALUES (?, ?)", (rowid, content))
                conn.execute("INSERT INTO short_index(rowid, tokens) VALUES (?, ?)", (rowid, short_tokens))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        elapsed = time.perf_counter() - started
    finally:
        conn.close()
    return elapsed, short_token_bytes


def _baseline_search(conn: sqlite3.Connection, query: str) -> list[int]:
    return [
        int(row[0])
        for row in conn.execute(
            "SELECT rowid FROM cjk_index WHERE cjk_index MATCH ? ORDER BY rowid",
            (_baseline_query(query),),
        )
    ]


def _hybrid_search(conn: sqlite3.Connection, query: str) -> list[int]:
    if len(query) == 2:
        sql = "SELECT rowid FROM short_index WHERE short_index MATCH ? ORDER BY rowid"
        match = f'"{query.replace(chr(34), chr(34) * 2)}"'
    else:
        sql = "SELECT rowid FROM trigram_index WHERE trigram_index MATCH ? ORDER BY rowid"
        match = f'"{query.replace(chr(34), chr(34) * 2)}"'
    return [int(row[0]) for row in conn.execute(sql, (match,))]


def _measure(callable_, iterations: int) -> tuple[float, float, list[int]]:
    samples: list[float] = []
    result: list[int] = []
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

    parser = argparse.ArgumentParser(description="Compare positional bigram and native-trigram CJK indexing")
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
        hybrid_db = root / "hybrid.db"

        baseline_time, baseline_token_bytes = _build_baseline(
            baseline_db,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
        )
        hybrid_time, short_token_bytes = _build_hybrid(
            hybrid_db,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
        )
        baseline_size = _database_bytes(baseline_db)
        hybrid_size = _database_bytes(hybrid_db)

        print("DocSeek native-trigram + short-bigram CJK experiment")
        print(f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb}KiB")
        print(
            f"baseline: build={baseline_time:.3f}s db={baseline_size / 1024 / 1024:.2f}MiB "
            f"expanded_bigram_text={baseline_token_bytes / 1024 / 1024:.2f}MiB"
        )
        print(
            f"hybrid  : build={hybrid_time:.3f}s db={hybrid_size / 1024 / 1024:.2f}MiB "
            f"short_token_text={short_token_bytes / 1024 / 1024:.2f}MiB "
            f"build_speedup={baseline_time / hybrid_time:.2f}x db_ratio={hybrid_size / baseline_size:.3f}x"
        )

        baseline_conn = sqlite3.connect(baseline_db)
        hybrid_conn = sqlite3.connect(hybrid_db)
        try:
            for query in QUERIES:
                base_p50, base_p95, base_ids = _measure(
                    lambda q=query: _baseline_search(baseline_conn, q), args.iterations
                )
                hybrid_p50, hybrid_p95, hybrid_ids = _measure(
                    lambda q=query: _hybrid_search(hybrid_conn, q), args.iterations
                )
                print(
                    f"- {query!r}: exact={'yes' if base_ids == hybrid_ids else 'NO'} hits={len(base_ids):,}; "
                    f"baseline(p50={base_p50:.2f}, p95={base_p95:.2f})ms; "
                    f"hybrid(p50={hybrid_p50:.2f}, p95={hybrid_p95:.2f})ms"
                )
        finally:
            baseline_conn.close()
            hybrid_conn.close()


if __name__ == "__main__":
    main()
