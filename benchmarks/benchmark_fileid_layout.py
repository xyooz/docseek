from __future__ import annotations

import argparse
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def _cjk_bigrams(text: str) -> str:
    tokens: list[str] = []
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return " ".join(tokens)


def _payload(index: int, chunk_no: int, target_bytes: int) -> str:
    base = f"客户经理 信贷业务 身份证有效期 文件{index} 块{chunk_no} "
    filler = "银行办公资料 风险管理 客户服务 业务制度 local payload "
    pieces = [base]
    size = len(base.encode("utf-8"))
    unit = filler.encode("utf-8")
    while size + len(unit) <= target_bytes:
        pieces.append(filler)
        size += len(unit)
    if size < target_bytes:
        pieces.append("x" * (target_bytes - size))
    return "".join(pieces)


def _long_path(index: int, extension: str, target_chars: int) -> str:
    filename = f"业务制度_{index:06d}{extension}"
    prefix = "C:/benchmark/某某分行/业务管理部/年度资料/制度与操作手册/"
    filler = "归档目录/"
    value = prefix
    while len(value) + len(filename) < target_chars:
        value += filler
    return value + filename


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32768")
    return conn


def _create_schema(conn: sqlite3.Connection, *, use_file_id: bool) -> None:
    relation = "file_id INTEGER NOT NULL" if use_file_id else "path TEXT NOT NULL"
    unique = "UNIQUE(file_id, ordinal)" if use_file_id else "UNIQUE(path, ordinal)"
    relation_index = "file_id" if use_file_id else "path"
    conn.executescript(
        f"""
        CREATE TABLE files(
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            extension TEXT NOT NULL,
            modified_time REAL NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE TABLE chunks(
            id INTEGER PRIMARY KEY,
            {relation},
            ordinal INTEGER NOT NULL,
            location TEXT NOT NULL,
            content TEXT NOT NULL,
            {unique}
        );
        CREATE INDEX idx_chunks_relation ON chunks({relation_index});
        CREATE VIRTUAL TABLE chunk_index_cjk2 USING fts5(
            filename_tokens,
            content_tokens,
            content='',
            tokenize='unicode61'
        );
        """
    )


def _build(
    db_path: Path,
    *,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
    path_chars: int,
    use_file_id: bool,
) -> float:
    conn = _connect(db_path)
    _create_schema(conn, use_file_id=use_file_id)
    payload_bytes = payload_kb * 1024
    started = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for index in range(files):
            extension = ".pdf" if index % 2 == 0 else ".docx"
            filename = f"业务制度_{index:06d}{extension}"
            path = _long_path(index, extension, path_chars)
            cursor = conn.execute(
                "INSERT INTO files(path, filename, extension, modified_time, size) VALUES (?, ?, ?, ?, ?)",
                (path, filename, extension, float(index), payload_bytes * chunks_per_file),
            )
            file_id = int(cursor.lastrowid)
            filename_tokens = _cjk_bigrams(filename)
            for chunk_no in range(chunks_per_file):
                content = _payload(index, chunk_no, payload_bytes)
                if use_file_id:
                    chunk_cursor = conn.execute(
                        "INSERT INTO chunks(file_id, ordinal, location, content) VALUES (?, ?, ?, ?)",
                        (file_id, chunk_no, f"块 {chunk_no + 1}", content),
                    )
                else:
                    chunk_cursor = conn.execute(
                        "INSERT INTO chunks(path, ordinal, location, content) VALUES (?, ?, ?, ?)",
                        (path, chunk_no, f"块 {chunk_no + 1}", content),
                    )
                chunk_id = int(chunk_cursor.lastrowid)
                conn.execute(
                    "INSERT INTO chunk_index_cjk2(rowid, filename_tokens, content_tokens) VALUES (?, ?, ?)",
                    (chunk_id, filename_tokens, _cjk_bigrams(content)),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    elapsed = time.perf_counter() - started
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return elapsed


def _query_sql(*, use_file_id: bool) -> str:
    join_files = "JOIN files f ON f.id = c.file_id" if use_file_id else "JOIN files f ON f.path = c.path"
    partition = "f.id" if use_file_id else "f.path"
    return f"""
        WITH hits AS (
            SELECT
                c.id AS chunk_id,
                f.path,
                f.modified_time,
                c.ordinal,
                bm25(chunk_index_cjk2, 5.0, 1.0) AS score
            FROM chunk_index_cjk2
            JOIN chunks c ON c.id = chunk_index_cjk2.rowid
            {join_files}
            WHERE chunk_index_cjk2 MATCH :query
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY {partition}
                    ORDER BY score ASC, ordinal ASC
                ) AS file_rank
            FROM hits
        )
        SELECT path, score
        FROM ranked
        WHERE file_rank = 1
        ORDER BY score ASC, modified_time DESC, path ASC
        LIMIT :limit
    """


def _measure(db_path: Path, *, use_file_id: bool, iterations: int, limit: int) -> tuple[float, float, list[str]]:
    conn = _connect(db_path)
    sql = _query_sql(use_file_id=use_file_id)
    params = {"query": '"客户 户经 经理"', "limit": limit}
    conn.execute(sql, params).fetchall()
    samples: list[float] = []
    rows = []
    for _ in range(iterations):
        started = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        samples.append((time.perf_counter() - started) * 1000.0)
    conn.close()
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index], [str(row[0]) for row in rows]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compare TEXT path vs INTEGER file_id chunk layouts")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--path-chars", type=int, default=120)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        path_db = root / "path-layout.db"
        id_db = root / "fileid-layout.db"
        path_build = _build(
            path_db,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            path_chars=args.path_chars,
            use_file_id=False,
        )
        id_build = _build(
            id_db,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            path_chars=args.path_chars,
            use_file_id=True,
        )
        path_p50, path_p95, path_results = _measure(
            path_db, use_file_id=False, iterations=args.iterations, limit=args.limit
        )
        id_p50, id_p95, id_results = _measure(
            id_db, use_file_id=True, iterations=args.iterations, limit=args.limit
        )
        path_size = path_db.stat().st_size
        id_size = id_db.stat().st_size

        print("DocSeek chunk relation layout benchmark")
        print(
            f"files={args.files:,} chunks/file={args.chunks} payload={args.payload_kb}KiB "
            f"path_chars~={args.path_chars}"
        )
        print(
            f"TEXT path: build={path_build:.3f}s db={path_size / 1024 / 1024:.2f}MiB "
            f"query(p50={path_p50:.2f}, p95={path_p95:.2f})ms"
        )
        print(
            f"INTEGER file_id: build={id_build:.3f}s db={id_size / 1024 / 1024:.2f}MiB "
            f"query(p50={id_p50:.2f}, p95={id_p95:.2f})ms"
        )
        print(
            f"size_ratio={id_size / path_size:.3f}x "
            f"query_speedup={path_p50 / id_p50 if id_p50 else 0.0:.2f}x "
            f"exact_match={'yes' if path_results == id_results else 'NO'}"
        )


if __name__ == "__main__":
    main()
