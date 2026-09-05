from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import tempfile
import time
import zlib
from pathlib import Path

from docseek.chunk_store import ChunkStore


_VOCABULARY = (
    "农业银行", "客户经理", "业务流程", "风险管理", "操作规范", "服务记录", "账户管理", "交易审核",
    "资料归档", "客户服务", "身份核验", "授权管理", "产品信息", "审批流程", "业务申请", "合规检查",
    "风险提示", "系统操作", "数据维护", "网点服务", "柜面业务", "电子渠道", "移动银行", "跨境业务",
    "结算账户", "贷款申请", "授信管理", "合同资料", "客户信息", "机构信息", "交易明细", "业务状态",
    "复核人员", "经办人员", "处理时间", "申请日期", "有效期限", "联系电话", "证件号码", "业务编号",
    "受理机构", "处理结果", "异常情况", "整改要求", "制度依据", "操作步骤", "注意事项", "业务场景",
    "审批意见", "风险等级", "客户类型", "账户状态", "产品类别", "渠道类型", "交易金额", "币种信息",
    "内部管理", "业务部门", "研发中心", "运维支持", "安全审计", "日志记录", "数据查询", "报表统计",
)


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
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


def _payload(index: int, ordinal: int, payload_bytes: int, *, profile: str) -> str:
    terms = [f"文档编号 {index} 内容块 {ordinal}"]
    if index % 4 == 0 and ordinal == 0:
        terms.append("业务流程")
    if index % 12 == 0 and ordinal == 1:
        terms.append("身份证有效期")
    if index % 200 == 0 and ordinal == 2:
        terms.append("跨境专项复核")

    if profile == "repetitive":
        seed = " ".join(terms) + " 本地办公资料 业务说明 操作规范 风险提示 服务记录 文档归档 客户服务 "
        filler = "农业银行 本地文档 检索系统 操作说明 风险管理 客户经理 服务流程 数据归档 "
        parts = [seed]
        current = len(seed.encode("utf-8"))
        filler_bytes = len(filler.encode("utf-8"))
        while current + filler_bytes <= payload_bytes:
            parts.append(filler)
            current += filler_bytes
        if current < payload_bytes:
            parts.append("x" * (payload_bytes - current))
        encoded = "".join(parts).encode("utf-8")[:payload_bytes]
        return encoded.decode("utf-8", errors="ignore")

    # A deterministic, higher-entropy office-like profile. Common business
    # vocabulary still repeats, but sequence, IDs, dates, amounts and reference
    # codes vary heavily between chunks. This intentionally makes zlib work
    # harder than the repetitive baseline without becoming random binary data.
    parts = [" ".join(terms), " "]
    current = len("".join(parts).encode("utf-8"))
    state = ((index + 1) * 0x9E3779B1) ^ ((ordinal + 7) * 0x85EBCA6B)
    token_no = 0
    while current < payload_bytes:
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        word = _VOCABULARY[state % len(_VOCABULARY)]
        if token_no % 11 == 0:
            token = f"{word} REF-{index:06d}-{ordinal:02d}-{state:08X} "
        elif token_no % 13 == 0:
            month = 1 + (state % 12)
            day = 1 + ((state >> 8) % 28)
            token = f"{word} 2026-{month:02d}-{day:02d} "
        elif token_no % 17 == 0:
            amount = (state % 9_000_000) / 100
            token = f"{word} {amount:.2f}元 "
        else:
            token = f"{word} "
        token_bytes = len(token.encode("utf-8"))
        if current + token_bytes > payload_bytes:
            remaining = payload_bytes - current
            if remaining > 0:
                parts.append("z" * remaining)
            break
        parts.append(token)
        current += token_bytes
        token_no += 1
    encoded = "".join(parts).encode("utf-8")[:payload_bytes]
    return encoded.decode("utf-8", errors="ignore")


def _create_schema(conn: sqlite3.Connection, *, compressed: bool) -> None:
    content_type = "BLOB" if compressed else "TEXT"
    conn.executescript(
        f"""
        CREATE TABLE files(
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL
        );
        CREATE TABLE chunks(
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            content {content_type} NOT NULL,
            UNIQUE(file_id, ordinal)
        );
        CREATE INDEX idx_chunks_file_id ON chunks(file_id);
        CREATE VIRTUAL TABLE chunk_index USING fts5(
            filename,
            content,
            content='',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE VIRTUAL TABLE chunk_index_cjk2 USING fts5(
            filename_tokens,
            content_tokens,
            content='',
            tokenize='unicode61'
        );
        """
    )


def _encode_content(text: str, *, compressed: bool) -> str | bytes:
    if not compressed:
        return text
    return zlib.compress(text.encode("utf-8"), level=1)


def _decode_content(value: object, *, compressed: bool) -> str:
    if not compressed:
        return str(value or "")
    return zlib.decompress(bytes(value)).decode("utf-8")


def _build(
    path: Path,
    *,
    compressed: bool,
    files: int,
    chunks_per_file: int,
    payload_kb: int,
    profile: str,
) -> tuple[float, int, int]:
    conn = sqlite3.connect(path)
    _configure(conn)
    _create_schema(conn, compressed=compressed)
    payload_bytes = payload_kb * 1024
    logical_bytes = 0
    started = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    for index in range(files):
        filename = f"2026年广州研发办公资料_业务操作规范_{index:06d}.pdf"
        file_id = int(conn.execute("INSERT INTO files(filename) VALUES (?)", (filename,)).lastrowid)
        for ordinal in range(chunks_per_file):
            content = _payload(index, ordinal, payload_bytes, profile=profile)
            logical_bytes += len(content.encode("utf-8"))
            chunk_id = int(
                conn.execute(
                    "INSERT INTO chunks(file_id, ordinal, content) VALUES (?, ?, ?)",
                    (file_id, ordinal, _encode_content(content, compressed=compressed)),
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, ?, ?)",
                (chunk_id, filename, content),
            )
            conn.execute(
                "INSERT INTO chunk_index_cjk2(rowid, filename_tokens, content_tokens) VALUES (?, ?, ?)",
                (chunk_id, ChunkStore._cjk_bigrams(filename), ChunkStore._cjk_bigrams(content)),
            )
    conn.commit()
    elapsed = time.perf_counter() - started
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return elapsed, _db_bytes(path), logical_bytes


def _cjk_phrase(text: str) -> str:
    return '"' + " ".join(ChunkStore._cjk_bigrams(text).split()) + '"'


def _search_page(conn: sqlite3.Connection, query: str, *, compressed: bool, limit: int) -> tuple[list[tuple[int, int]], int]:
    phrase = _cjk_phrase(query)
    rows = conn.execute(
        """
        WITH hits AS (
            SELECT
                c.id AS chunk_id,
                c.file_id,
                c.ordinal,
                bm25(chunk_index_cjk2, 5.0, 1.0) AS score
            FROM chunk_index_cjk2
            JOIN chunks c ON c.id = chunk_index_cjk2.rowid
            WHERE chunk_index_cjk2 MATCH :query
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY file_id
                    ORDER BY score ASC, ordinal ASC
                ) AS file_rank
            FROM hits
        ),
        page AS (
            SELECT chunk_id, file_id, ordinal, score, COUNT(*) OVER() AS total_count
            FROM ranked
            WHERE file_rank = 1
            ORDER BY score ASC, file_id ASC
            LIMIT :limit
        )
        SELECT page.chunk_id, page.file_id, page.ordinal, page.total_count, chunks.content
        FROM page
        JOIN chunks ON chunks.id = page.chunk_id
        ORDER BY page.score ASC, page.file_id ASC
        """,
        {"query": phrase, "limit": limit},
    ).fetchall()
    for row in rows:
        _decode_content(row["content"], compressed=compressed)
    total = int(rows[0]["total_count"]) if rows else 0
    return [(int(row["file_id"]), int(row["ordinal"])) for row in rows], total


def _measure(fn, *, iterations: int) -> tuple[float, float, object]:
    value = fn()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        value = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return statistics.median(samples), ordered[p95_index], value


def _delete_document(conn: sqlite3.Connection, file_id: int, *, compressed: bool) -> None:
    row = conn.execute("SELECT filename FROM files WHERE id = ?", (file_id,)).fetchone()
    filename = str(row["filename"])
    chunks = conn.execute(
        "SELECT id, content FROM chunks WHERE file_id = ? ORDER BY id",
        (file_id,),
    ).fetchall()
    for chunk in chunks:
        chunk_id = int(chunk["id"])
        content = _decode_content(chunk["content"], compressed=compressed)
        conn.execute(
            "INSERT INTO chunk_index(chunk_index, rowid, filename, content) VALUES ('delete', ?, ?, ?)",
            (chunk_id, filename, content),
        )
        conn.execute(
            """
            INSERT INTO chunk_index_cjk2(
                chunk_index_cjk2, rowid, filename_tokens, content_tokens
            ) VALUES ('delete', ?, ?, ?)
            """,
            (chunk_id, ChunkStore._cjk_bigrams(filename), ChunkStore._cjk_bigrams(content)),
        )
    conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))


def _measure_delete(path: Path, *, compressed: bool, documents: int) -> float:
    conn = sqlite3.connect(path)
    _configure(conn)
    started = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    for file_id in range(1, documents + 1):
        _delete_document(conn, file_id, compressed=compressed)
    conn.commit()
    elapsed = time.perf_counter() - started
    conn.close()
    return elapsed


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compare TEXT and zlib-compressed raw chunk storage")
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--delete-documents", type=int, default=100)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--profile", choices=("repetitive", "varied"), default="repetitive")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        raw_path = root / "raw.db"
        compressed_path = root / "compressed.db"
        raw_build, raw_bytes, logical_bytes = _build(
            raw_path,
            compressed=False,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            profile=args.profile,
        )
        comp_build, comp_bytes, comp_logical = _build(
            compressed_path,
            compressed=True,
            files=args.files,
            chunks_per_file=args.chunks,
            payload_kb=args.payload_kb,
            profile=args.profile,
        )
        if logical_bytes != comp_logical:
            raise SystemExit("logical source mismatch")

        raw_conn = sqlite3.connect(raw_path)
        comp_conn = sqlite3.connect(compressed_path)
        _configure(raw_conn)
        _configure(comp_conn)
        try:
            results = []
            for label, query in (("common", "业务流程"), ("medium", "身份证有效期"), ("rare", "跨境专项复核")):
                raw = _measure(
                    lambda q=query: _search_page(raw_conn, q, compressed=False, limit=args.limit),
                    iterations=args.iterations,
                )
                comp = _measure(
                    lambda q=query: _search_page(comp_conn, q, compressed=True, limit=args.limit),
                    iterations=args.iterations,
                )
                if raw[2] != comp[2]:
                    raise SystemExit(f"search mismatch for {label}")
                results.append((label, raw, comp))
        finally:
            raw_conn.close()
            comp_conn.close()

        delete_n = min(args.delete_documents, args.files)
        raw_delete = _measure_delete(raw_path, compressed=False, documents=delete_n)
        comp_delete = _measure_delete(compressed_path, compressed=True, documents=delete_n)

        print("DocSeek raw chunk compression A/B")
        print(
            f"profile={args.profile} files={args.files:,} chunks/file={args.chunks} "
            f"payload/chunk~={args.payload_kb}KiB logical_source={logical_bytes / 1024 / 1024:.2f}MiB"
        )
        print(f"raw:        build={raw_build:.3f}s db={raw_bytes / 1024 / 1024:.2f}MiB")
        print(f"compressed: build={comp_build:.3f}s db={comp_bytes / 1024 / 1024:.2f}MiB")
        print(
            f"size_ratio={comp_bytes / raw_bytes:.3f} "
            f"build_speedup={raw_build / comp_build:.2f}x"
        )
        for label, raw, comp in results:
            print(
                f"- {label:8s} raw(p50={raw[0]:6.2f}, p95={raw[1]:6.2f})ms "
                f"compressed(p50={comp[0]:6.2f}, p95={comp[1]:6.2f})ms "
                f"speedup={raw[0] / comp[0]:4.2f}x"
            )
        print(
            f"delete_{delete_n}_docs: raw={raw_delete * 1000:.2f}ms "
            f"compressed={comp_delete * 1000:.2f}ms ratio={comp_delete / raw_delete:.2f}x"
        )


if __name__ == "__main__":
    main()
