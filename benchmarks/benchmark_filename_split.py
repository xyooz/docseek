from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from docseek.chunk_store import ChunkStore


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32768")


def _db_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )


def _filename(index: int) -> str:
    marker = "专项检查_" if index % 10 == 0 else ""
    return (
        f"2026年广州研发办公资料_{marker}客户服务与风险管理业务操作指引_"
        f"{index:06d}.pdf"
    )


def _payload(index: int, ordinal: int, payload_bytes: int) -> str:
    markers: list[str] = []
    if index % 4 == 0 and ordinal == 0:
        markers.append("业务流程")
    if index % 20 == 0 and ordinal == 1:
        markers.append("客户经理 信贷政策")
    if index % 50 == 0 and ordinal == 2:
        markers.append("跨境专项复核")
    if index % 10 == 0 and ordinal == 0:
        # This term is paired with filename-only 专项检查.
        markers.append("风险整改")

    base = " ".join(markers) + f" 文档 {index} 内容块 {ordinal} 银行业务资料 "
    filler = "客户服务 操作规范 风险管理 本地检索 office document payload "
    pieces = [base]
    current = len(base.encode("utf-8"))
    filler_bytes = filler.encode("utf-8")
    while current + len(filler_bytes) <= payload_bytes:
        pieces.append(filler)
        current += len(filler_bytes)
    if current < payload_bytes:
        pieces.append("x" * (payload_bytes - current))
    return "".join(pieces)


def _create_common(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE files(id INTEGER PRIMARY KEY, filename TEXT NOT NULL);
        CREATE TABLE chunks(
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            content TEXT NOT NULL,
            UNIQUE(file_id, ordinal)
        );
        CREATE INDEX idx_chunks_file_id ON chunks(file_id);
        """
    )


def _create_repeated(conn: sqlite3.Connection) -> None:
    _create_common(conn)
    conn.executescript(
        """
        CREATE VIRTUAL TABLE chunk_index USING fts5(
            filename, content, content='', tokenize='unicode61 remove_diacritics 2'
        );
        CREATE VIRTUAL TABLE chunk_index_cjk2 USING fts5(
            filename_tokens, content_tokens, content='', tokenize='unicode61'
        );
        """
    )


def _create_split(conn: sqlite3.Connection) -> None:
    _create_common(conn)
    conn.executescript(
        """
        CREATE VIRTUAL TABLE filename_index USING fts5(
            filename, content='', tokenize='unicode61 remove_diacritics 2'
        );
        CREATE VIRTUAL TABLE filename_index_cjk2 USING fts5(
            filename_tokens, content='', tokenize='unicode61'
        );
        CREATE VIRTUAL TABLE content_index USING fts5(
            content, content='', tokenize='unicode61 remove_diacritics 2'
        );
        CREATE VIRTUAL TABLE content_index_cjk2 USING fts5(
            content_tokens, content='', tokenize='unicode61'
        );
        """
    )


def _build(
    path: Path,
    *,
    split: bool,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
) -> tuple[float, int]:
    conn = sqlite3.connect(path)
    _configure(conn)
    (_create_split if split else _create_repeated)(conn)
    payload_bytes = max(1, payload_kb) * 1024

    started = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    for index in range(files):
        filename = _filename(index)
        file_id = int(conn.execute("INSERT INTO files(filename) VALUES (?)", (filename,)).lastrowid)
        filename_tokens = ChunkStore._cjk_bigrams(filename)
        if split:
            conn.execute(
                "INSERT INTO filename_index(rowid, filename) VALUES (?, ?)",
                (file_id, filename),
            )
            conn.execute(
                "INSERT INTO filename_index_cjk2(rowid, filename_tokens) VALUES (?, ?)",
                (file_id, filename_tokens),
            )

        for ordinal in range(chunks_per_file):
            content = _payload(index, ordinal, payload_bytes)
            chunk_id = int(
                conn.execute(
                    "INSERT INTO chunks(file_id, ordinal, content) VALUES (?, ?, ?)",
                    (file_id, ordinal, content),
                ).lastrowid
            )
            content_tokens = ChunkStore._cjk_bigrams(content)
            if split:
                conn.execute(
                    "INSERT INTO content_index(rowid, content) VALUES (?, ?)",
                    (chunk_id, content),
                )
                conn.execute(
                    "INSERT INTO content_index_cjk2(rowid, content_tokens) VALUES (?, ?)",
                    (chunk_id, content_tokens),
                )
            else:
                conn.execute(
                    "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, ?, ?)",
                    (chunk_id, filename, content),
                )
                conn.execute(
                    """
                    INSERT INTO chunk_index_cjk2(rowid, filename_tokens, content_tokens)
                    VALUES (?, ?, ?)
                    """,
                    (chunk_id, filename_tokens, content_tokens),
                )
    conn.commit()
    elapsed = time.perf_counter() - started
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return elapsed, _db_bytes(path)


def _phrase(text: str) -> str:
    return '"' + " ".join(ChunkStore._cjk_bigrams(text).split()) + '"'


def _and_query(*terms: str) -> str:
    return " AND ".join(_phrase(term) for term in terms)


def _repeated_count(conn: sqlite3.Connection, *terms: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT c.file_id)
        FROM chunk_index_cjk2
        JOIN chunks c ON c.id = chunk_index_cjk2.rowid
        WHERE chunk_index_cjk2 MATCH ?
        """,
        (_and_query(*terms),),
    ).fetchone()
    return int(row[0])


def _split_filename_count(conn: sqlite3.Connection, term: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM filename_index_cjk2 WHERE filename_index_cjk2 MATCH ?",
            (_phrase(term),),
        ).fetchone()[0]
    )


def _split_content_count(conn: sqlite3.Connection, *terms: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT c.file_id)
            FROM content_index_cjk2
            JOIN chunks c ON c.id = content_index_cjk2.rowid
            WHERE content_index_cjk2 MATCH ?
            """,
            (_and_query(*terms),),
        ).fetchone()[0]
    )


def _split_cross_source_count(
    conn: sqlite3.Connection,
    filename_term: str,
    content_term: str,
) -> int:
    return int(
        conn.execute(
            """
            WITH filename_hits AS (
                SELECT rowid AS file_id
                FROM filename_index_cjk2
                WHERE filename_index_cjk2 MATCH :filename_query
            ),
            content_hits AS (
                SELECT DISTINCT c.file_id
                FROM content_index_cjk2
                JOIN chunks c ON c.id = content_index_cjk2.rowid
                WHERE content_index_cjk2 MATCH :content_query
            )
            SELECT COUNT(*)
            FROM filename_hits f
            JOIN content_hits c ON c.file_id = f.file_id
            """,
            {
                "filename_query": _phrase(filename_term),
                "content_query": _phrase(content_term),
            },
        ).fetchone()[0]
    )


def _measure(fn, *, iterations: int) -> tuple[float, float, int]:
    fn()
    samples: list[float] = []
    result = 0
    for _ in range(iterations):
        started = time.perf_counter()
        result = int(fn())
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index], result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Compare repeated per-chunk filename FTS with split file/content FTS"
    )
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--chunks", type=int, default=12)
    parser.add_argument("--payload-kb", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        repeated_path = root / "repeated.db"
        split_path = root / "split.db"
        repeated_time, repeated_bytes = _build(
            repeated_path,
            split=False,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
        )
        split_time, split_bytes = _build(
            split_path,
            split=True,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
        )

        repeated = sqlite3.connect(repeated_path)
        split = sqlite3.connect(split_path)
        _configure(repeated)
        _configure(split)
        try:
            cases = [
                (
                    "filename-only",
                    _measure(lambda: _repeated_count(repeated, "专项检查"), iterations=args.iterations),
                    _measure(lambda: _split_filename_count(split, "专项检查"), iterations=args.iterations),
                ),
                (
                    "content-only",
                    _measure(lambda: _repeated_count(repeated, "跨境专项复核"), iterations=args.iterations),
                    _measure(lambda: _split_content_count(split, "跨境专项复核"), iterations=args.iterations),
                ),
                (
                    "content-multiterm",
                    _measure(lambda: _repeated_count(repeated, "客户经理", "信贷政策"), iterations=args.iterations),
                    _measure(lambda: _split_content_count(split, "客户经理", "信贷政策"), iterations=args.iterations),
                ),
                (
                    "cross-source-and",
                    _measure(lambda: _repeated_count(repeated, "专项检查", "风险整改"), iterations=args.iterations),
                    _measure(
                        lambda: _split_cross_source_count(split, "专项检查", "风险整改"),
                        iterations=args.iterations,
                    ),
                ),
            ]
        finally:
            repeated.close()
            split.close()

        print("DocSeek filename index split A/B")
        print(f"files={args.files:,} chunks/file={args.chunks} payload/chunk~={args.payload_kb}KiB")
        print(f"repeated: index={repeated_time:.3f}s db={repeated_bytes / 1024 / 1024:.2f}MiB")
        print(f"split:    index={split_time:.3f}s db={split_bytes / 1024 / 1024:.2f}MiB")
        print(
            f"size_ratio={split_bytes / repeated_bytes:.3f} "
            f"index_speedup={repeated_time / split_time:.2f}x"
        )

        for label, old, new in cases:
            old_p50, old_p95, old_count = old
            new_p50, new_p95, new_count = new
            print(
                f"{label:18s} repeated(p50={old_p50:.2f}, p95={old_p95:.2f}, n={old_count})ms "
                f"split(p50={new_p50:.2f}, p95={new_p95:.2f}, n={new_count})ms "
                f"count_match={'yes' if old_count == new_count else 'NO'}"
            )
            if old_count != new_count:
                raise SystemExit(f"semantic count mismatch for {label}: {old_count} != {new_count}")


if __name__ == "__main__":
    main()
