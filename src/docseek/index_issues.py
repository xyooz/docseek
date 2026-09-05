from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass(slots=True)
class IndexIssue:
    path: str
    error_code: str
    detail: str
    updated_at: float


ERROR_LABELS = {
    "file_too_large": "文件过大",
    "permission_denied": "无访问权限",
    "file_not_found": "文件已不存在",
    "BadZipFile": "Office 文件损坏或格式异常",
    "FileDataError": "文件数据异常",
    "EmptyFileError": "空文件或无法解析",
    "os_error": "系统访问错误",
}


class IndexIssueStore:
    """Persistent index problems independent from successfully indexed files."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            # journal_mode changes are relatively expensive on Windows; set it
            # once instead of on every clear/record connection.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS index_issues (
                    path TEXT PRIMARY KEY,
                    error_code TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_index_issues_code ON index_issues(error_code)"
            )

    def record(self, path: str, error_code: str, detail: str = "") -> None:
        self.record_many([(path, error_code, detail)])

    def record_many(self, issues: Iterable[tuple[str, str, str]]) -> None:
        rows = [
            (path, error_code, detail[:1000], time.time())
            for path, error_code, detail in issues
        ]
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO index_issues(path, error_code, detail, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    error_code=excluded.error_code,
                    detail=excluded.detail,
                    updated_at=excluded.updated_at
                """,
                rows,
            )

    def clear(self, path: str) -> None:
        self.clear_many([path])

    def clear_many(self, paths: Iterable[str]) -> None:
        unique = list(dict.fromkeys(paths))
        if not unique:
            return
        with self.connect() as conn:
            conn.executemany(
                "DELETE FROM index_issues WHERE path = ?",
                [(path,) for path in unique],
            )

    def clear_under_root_if_missing(self, root: str, existing_paths: set[str]) -> int:
        """Remove stale issues only when absence can be established safely.

        Permission/system access errors are intentionally retained because on
        Windows an inaccessible path may also appear not to exist.
        """
        root_path = Path(root).resolve()
        with self.connect() as conn:
            rows = conn.execute("SELECT path, error_code FROM index_issues").fetchall()

        stale: list[str] = []
        for row in rows:
            issue_path = str(row["path"])
            error_code = str(row["error_code"])
            candidate = Path(issue_path)
            try:
                candidate.relative_to(root_path)
            except ValueError:
                continue
            if issue_path in existing_paths:
                continue
            if error_code in {"permission_denied", "os_error"}:
                continue
            try:
                exists = candidate.exists()
            except OSError:
                continue
            if not exists:
                stale.append(issue_path)

        self.clear_many(stale)
        return len(stale)

    def list(self, *, limit: int = 500) -> list[IndexIssue]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT path, error_code, detail, updated_at
                FROM index_issues
                ORDER BY updated_at DESC, path ASC
                LIMIT ?
                """,
                (max(1, min(int(limit), 5000)),),
            ).fetchall()
        return [
            IndexIssue(
                path=str(row["path"]),
                error_code=str(row["error_code"]),
                detail=str(row["detail"] or ""),
                updated_at=float(row["updated_at"]),
            )
            for row in rows
        ]

    def count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM index_issues").fetchone()
        return int(row[0])

    def summary(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT error_code, COUNT(*) AS n FROM index_issues GROUP BY error_code"
            ).fetchall()
        return {str(row["error_code"]): int(row["n"]) for row in rows}


def issue_label(error_code: str) -> str:
    return ERROR_LABELS.get(error_code, error_code)
