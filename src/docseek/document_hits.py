"""Exact, bounded pages of matching chunks within a selected document."""
from __future__ import annotations

import sqlite3
import time

from .chunk_store import ChunkSearchResult
from .chunks import DocumentChunk
from .structure_ranking import parse_structure_query
from .structure_store import FIELDS, structure_for_chunk


def document_hits(store, selected, query, *, offset=0, limit=30, cancelled=None):
    limit = max(1, min(50, int(limit)))
    text = parse_structure_query(query).text
    table, expression = store._select_index(text)
    deadline = time.monotonic() + 5
    with store.connect() as conn:
        conn.execute("PRAGMA busy_timeout=1000")
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline or
                                             (cancelled is not None and cancelled())), 1000)
        try:
            # One read snapshot prevents an index replacement between checking
            # the selected file and retrieving its chunks.
            conn.execute("BEGIN")
            current = conn.execute("SELECT * FROM files WHERE path=?", (selected.path,)).fetchone()
            if current is None or (current["modified_time"], current["size"]) != (selected.modified_time, selected.size):
                raise ValueError("文件索引已更新或移除，请重新搜索后查看命中。")
            rows = conn.execute(f"""
                SELECT c.id, c.ordinal, c.location, c.content,
                       {', '.join('s.' + name for name in FIELDS)}
                FROM {table}
                JOIN chunks c ON c.id={table}.rowid
                LEFT JOIN chunk_structure s ON s.chunk_id=c.id
                WHERE {table} MATCH ? AND c.file_id=?
                ORDER BY c.ordinal, c.id LIMIT ? OFFSET ?
            """, (expression, current["id"], limit + 1, max(0, int(offset)))).fetchall()
            items = []
            for row in rows[:limit]:
                if cancelled is not None and cancelled():
                    raise ValueError("已取消")
                content = store.decode_content(row["content"])
                structure = ({name: row[name] for name in FIELDS} if row["kind"] is not None else
                             structure_for_chunk(selected.path, selected.extension,
                                                 DocumentChunk(row["ordinal"], row["location"], "")))
                items.append(ChunkSearchResult(
                    selected.path, selected.filename, selected.extension, selected.modified_time,
                    selected.size, row["location"], store._snippet_from_content(content, text),
                    0, int(row["id"]), structure))
            return items, len(rows) > limit
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise ValueError("命中加载已取消或超过 5 秒，请缩小关键词范围后重试。") from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)
            conn.rollback()
