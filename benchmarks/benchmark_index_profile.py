from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from benchmark_search import _database_bytes, _payload_text
from docseek.chunk_codec import encode_chunk_content
from docseek.chunk_store import ChunkStore
from docseek.search_db import SearchDatabase


def _measure(timers: dict[str, float], key: str, fn):
    started = time.perf_counter()
    value = fn()
    timers[key] += time.perf_counter() - started
    return value


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Profile DocSeek first-index component costs")
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()

    files = max(1, args.files)
    chunks_per_file = max(1, args.chunks)
    payload_bytes = max(1, args.payload_kb) * 1024
    batch_size = max(1, args.batch_size)

    timers: dict[str, float] = defaultdict(float)
    logical_bytes = 0
    stored_raw_bytes = 0

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "profile.db"
        SearchDatabase(db_path)
        store = ChunkStore(db_path)

        wall_started = time.perf_counter()
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = 0
            for index in range(files):
                extension = ".pdf" if index % 2 == 0 else ".docx"
                filename = f"业务制度_{index:06d}{extension}"
                path = str(Path("C:/benchmark") / filename)

                def insert_file() -> int:
                    cursor = conn.execute(
                        """
                        INSERT INTO files(path, filename, extension, modified_time, size, last_error)
                        VALUES (?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            path,
                            filename,
                            extension,
                            float(index),
                            payload_bytes * chunks_per_file,
                        ),
                    )
                    return int(cursor.lastrowid)

                file_id = _measure(timers, "file metadata SQL", insert_file)
                filename_tokens = _measure(
                    timers,
                    "CJK token generation",
                    lambda: store._cjk_bigrams(filename),
                )

                for chunk_no in range(chunks_per_file):
                    content = _measure(
                        timers,
                        "synthetic payload generation",
                        lambda i=index, c=chunk_no: _payload_text(i, c, payload_bytes),
                    )
                    content_bytes = content.encode("utf-8")
                    logical_bytes += len(content_bytes)
                    encoded = _measure(
                        timers,
                        "zlib raw-copy encode",
                        lambda text=content: encode_chunk_content(text),
                    )
                    stored_raw_bytes += len(encoded)

                    def insert_chunk() -> int:
                        cursor = conn.execute(
                            """
                            INSERT INTO chunks(file_id, ordinal, location, content)
                            VALUES (?, ?, ?, ?)
                            """,
                            (file_id, chunk_no, f"块 {chunk_no + 1}", encoded),
                        )
                        return int(cursor.lastrowid)

                    chunk_id = _measure(timers, "chunks SQL", insert_chunk)
                    _measure(
                        timers,
                        "unicode FTS SQL",
                        lambda cid=chunk_id, text=content: conn.execute(
                            "INSERT INTO chunk_index(rowid, filename, content) VALUES (?, ?, ?)",
                            (cid, filename, text),
                        ),
                    )
                    content_tokens = _measure(
                        timers,
                        "CJK token generation",
                        lambda text=content: store._cjk_bigrams(text),
                    )
                    _measure(
                        timers,
                        "CJK FTS SQL",
                        lambda cid=chunk_id, tokens=content_tokens: conn.execute(
                            """
                            INSERT INTO chunk_index_cjk2(rowid, filename_tokens, content_tokens)
                            VALUES (?, ?, ?)
                            """,
                            (cid, filename_tokens, tokens),
                        ),
                    )

                pending += 1
                if pending >= batch_size:
                    _measure(timers, "batch commit", conn.commit)
                    conn.execute("BEGIN IMMEDIATE")
                    pending = 0

            if pending:
                _measure(timers, "batch commit", conn.commit)
            else:
                # The previous batch commit ended the explicit transaction.
                pass

            _measure(
                timers,
                "WAL checkpoint",
                lambda: conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall(),
            )

        wall = time.perf_counter() - wall_started
        db_bytes = _database_bytes(db_path)

    accounted = sum(timers.values())
    print("DocSeek first-index component profile")
    print(
        f"files={files:,} chunks/file={chunks_per_file} payload/chunk~={args.payload_kb}KiB "
        f"batch={batch_size}"
    )
    print(
        f"wall={wall:.3f}s files/s={files / wall:.1f} "
        f"source={logical_bytes / 1024 / 1024:.2f}MiB "
        f"db={db_bytes / 1024 / 1024:.2f}MiB"
    )
    print(
        f"raw_copy_ratio={stored_raw_bytes / logical_bytes if logical_bytes else 0.0:.3f}x "
        f"accounted={accounted:.3f}s ({accounted / wall * 100.0:.1f}% of wall)"
    )
    for key, seconds in sorted(timers.items(), key=lambda item: item[1], reverse=True):
        print(
            f"- {key:28s} {seconds:8.3f}s "
            f"{seconds / wall * 100.0:6.1f}% wall"
        )


if __name__ == "__main__":
    main()
