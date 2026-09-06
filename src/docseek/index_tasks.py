from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TASK_PENDING = "PENDING"
TASK_EXTRACTING = "EXTRACTING"
TASK_READY_TO_COMMIT = "READY_TO_COMMIT"
RECOVERABLE_TASK_STATES = (TASK_PENDING, TASK_EXTRACTING, TASK_READY_TO_COMMIT)
LANE_FAST = "fast"
LANE_COMPAT = "compat"


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass(frozen=True, slots=True)
class IndexTask:
    path: str
    lane: str
    state: str
    revision: int
    modified_time: float
    size: int
    attempts: int
    updated_at: float


class IndexTaskStore:
    """Small durable work ledger for interrupted indexing work.

    Extracted document text is deliberately *not* persisted here. The ledger
    records only source fingerprints and lifecycle state. If the process dies
    during parsing, the next process reparses the source instead of trusting a
    temporary spool containing sensitive office text.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
            factory=_ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_tasks(
                    path TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    modified_time REAL NOT NULL,
                    size INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_index_tasks_state_lane
                    ON index_tasks(state, lane, updated_at);
                """
            )

    def enqueue(
        self,
        path: str | Path,
        *,
        lane: str,
        revision: int,
        modified_time: float,
        size: int,
    ) -> None:
        normalized = str(path)
        now = time.time()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT revision, modified_time, size, attempts FROM index_tasks WHERE path = ?",
                (normalized,),
            ).fetchone()
            attempts = 0
            if existing is not None:
                same_source = (
                    int(existing["revision"]) == int(revision)
                    and float(existing["modified_time"]) == float(modified_time)
                    and int(existing["size"]) == int(size)
                )
                attempts = int(existing["attempts"]) if same_source else 0
            conn.execute(
                """
                INSERT INTO index_tasks(
                    path, lane, state, revision, modified_time, size, attempts, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    lane=excluded.lane,
                    state=excluded.state,
                    revision=excluded.revision,
                    modified_time=excluded.modified_time,
                    size=excluded.size,
                    attempts=excluded.attempts,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized,
                    lane,
                    TASK_PENDING,
                    int(revision),
                    float(modified_time),
                    int(size),
                    attempts,
                    now,
                ),
            )

    def mark_extracting(self, path: str | Path) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE index_tasks
                SET state = ?, attempts = attempts + 1, updated_at = ?
                WHERE path = ?
                """,
                (TASK_EXTRACTING, time.time(), str(path)),
            )

    def mark_ready_to_commit(self, path: str | Path) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE index_tasks SET state = ?, updated_at = ? WHERE path = ?",
                (TASK_READY_TO_COMMIT, time.time(), str(path)),
            )

    def complete(self, path: str | Path) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM index_tasks WHERE path = ?", (str(path),))

    def complete_many(self, paths: Iterable[str | Path]) -> None:
        unique = list(dict.fromkeys(str(path) for path in paths))
        if not unique:
            return
        with self.connect() as conn:
            conn.executemany(
                "DELETE FROM index_tasks WHERE path = ?",
                [(path,) for path in unique],
            )

    def recover_interrupted(self) -> list[IndexTask]:
        """Convert abandoned in-flight work back to PENDING and return the queue."""
        with self.connect() as conn:
            now = time.time()
            conn.execute(
                """
                UPDATE index_tasks
                SET state = ?, updated_at = ?
                WHERE state IN (?, ?)
                """,
                (
                    TASK_PENDING,
                    now,
                    TASK_EXTRACTING,
                    TASK_READY_TO_COMMIT,
                ),
            )
            rows = conn.execute(
                """
                SELECT path, lane, state, revision, modified_time, size, attempts, updated_at
                FROM index_tasks
                WHERE state = ?
                ORDER BY CASE lane WHEN 'fast' THEN 0 ELSE 1 END,
                         updated_at ASC,
                         path ASC
                """,
                (TASK_PENDING,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list(self) -> list[IndexTask]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT path, lane, state, revision, modified_time, size, attempts, updated_at
                FROM index_tasks
                ORDER BY CASE lane WHEN 'fast' THEN 0 ELSE 1 END,
                         updated_at ASC,
                         path ASC
                """
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM index_tasks").fetchone()
        return int(row[0])

    def remove_under_root(self, root: str | Path) -> int:
        root_path = Path(root).resolve()
        selected: list[str] = []
        with self.connect() as conn:
            rows = conn.execute("SELECT path FROM index_tasks").fetchall()
            for row in rows:
                task_path = str(row["path"])
                try:
                    Path(task_path).resolve().relative_to(root_path)
                except (ValueError, OSError):
                    continue
                selected.append(task_path)
            if selected:
                conn.executemany(
                    "DELETE FROM index_tasks WHERE path = ?",
                    [(path,) for path in selected],
                )
        return len(selected)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> IndexTask:
        return IndexTask(
            path=str(row["path"]),
            lane=str(row["lane"]),
            state=str(row["state"]),
            revision=int(row["revision"]),
            modified_time=float(row["modified_time"]),
            size=int(row["size"]),
            attempts=int(row["attempts"]),
            updated_at=float(row["updated_at"]),
        )
