from __future__ import annotations

import ctypes
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diagnostics import build_diagnostic_report
from .index_health import format_storage_size
from .search_db import SearchDatabase


BETA_SNAPSHOT_VERSION = 1


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except (FileNotFoundError, OSError):
        return 0


def _total_memory_bytes() -> int | None:
    """Best-effort physical-memory capacity without adding a psutil dependency."""

    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError):
            return None
        return int(status.ullTotalPhys) if ok else None

    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        physical_pages = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    total = page_size * physical_pages
    return total if total > 0 else None


def build_beta_snapshot(
    database: SearchDatabase,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a local Beta validation snapshot without document identifiers.

    The snapshot deliberately reuses the privacy-safe diagnostic report and only
    adds host-capacity and SQLite-file-size aggregates. It must not contain the
    database path, index-root paths, filenames, document text, issue details or
    search history.
    """

    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    diagnostic = build_diagnostic_report(database, generated_at=now)
    db_path = Path(database.db_path)
    db_bytes = _file_size(db_path)
    wal_bytes = _file_size(Path(f"{db_path}-wal"))
    shm_bytes = _file_size(Path(f"{db_path}-shm"))

    return {
        "snapshot_version": BETA_SNAPSHOT_VERSION,
        "generated_at_utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "diagnostic": diagnostic,
        "host_capacity": {
            "logical_cpu_count": os.cpu_count(),
            "physical_memory_bytes": _total_memory_bytes(),
        },
        "sqlite_files": {
            "database_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "shm_bytes": shm_bytes,
            "total_bytes": db_bytes + wal_bytes + shm_bytes,
        },
        "manual_fields": {
            "beta_stage": None,
            "source_file_count": None,
            "first_index_seconds": None,
            "second_reconcile_seconds": None,
            "peak_cpu_percent": None,
            "peak_memory_bytes": None,
            "query_count": None,
            "top1_hits": None,
            "top5_hits": None,
            "top10_hits": None,
            "queries_rewritten": None,
            "blocker_count": None,
            "major_count": None,
            "minor_count": None,
            "notes": None,
        },
        "privacy": {
            "contains_database_path": False,
            "contains_document_text": False,
            "contains_filenames": False,
            "contains_file_paths": False,
            "contains_index_root_paths": False,
            "contains_issue_details": False,
            "contains_search_history": False,
        },
    }


def beta_snapshot_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _value(value: Any) -> str:
    if value is None:
        return "待补录"
    return str(value)


def beta_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    diagnostic = snapshot["diagnostic"]
    docseek = diagnostic["docseek"]
    runtime = diagnostic["runtime"]
    index = diagnostic["index"]
    host = snapshot["host_capacity"]
    sqlite_files = snapshot["sqlite_files"]

    memory = host.get("physical_memory_bytes")
    memory_text = format_storage_size(int(memory)) if memory is not None else "未知"

    lines = [
        "# DocSeek Beta 验证快照",
        "",
        f"生成时间（UTC）：{snapshot['generated_at_utc']}",
        "",
        "## 自动采集",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| DocSeek 版本 | {docseek['version']} |",
        f"| Schema | {docseek['schema_actual']} / expected {docseek['schema_expected']} |",
        f"| Windows/OS | {runtime['os']} {runtime['os_release']} |",
        f"| OS 版本 | {runtime['os_version']} |",
        f"| 架构 | {runtime['architecture']} |",
        f"| Python | {runtime['python']} |",
        f"| SQLite | {runtime['sqlite']} |",
        f"| 逻辑 CPU | {_value(host.get('logical_cpu_count'))} |",
        f"| 物理内存 | {memory_text} |",
        f"| 已索引文件 | {index['indexed_files']} |",
        f"| 索引目录 | total {index['roots']['total']} / active {index['roots']['active']} / paused {index['roots']['paused']} |",
        f"| 索引问题 | {index['issues']['total']} |",
        f"| 最近完整校准（UTC） | {_value(index['last_successful_reconcile_at_utc'])} |",
        f"| DB | {format_storage_size(sqlite_files['database_bytes'])} |",
        f"| WAL | {format_storage_size(sqlite_files['wal_bytes'])} |",
        f"| SHM | {format_storage_size(sqlite_files['shm_bytes'])} |",
        f"| SQLite 总占用 | {format_storage_size(sqlite_files['total_bytes'])} |",
        "",
        "### 索引问题分类",
        "",
    ]

    issue_codes = index["issues"]["by_error_code"]
    if issue_codes:
        lines.extend(["| 错误代码 | 数量 |", "| --- | ---: |"])
        for code, count in sorted(issue_codes.items()):
            lines.append(f"| `{code}` | {count} |")
    else:
        lines.append("当前没有持久化索引问题。")

    lines.extend(
        [
            "",
            "## 人工补录",
            "",
            "| 项目 | 结果 |",
            "| --- | --- |",
            "| Beta 阶段 | A Smoke / B Scale / C Soak |",
            "| 真实源文件数 |  |",
            "| 首次索引耗时 |  |",
            "| 第二次无变化校准耗时 |  |",
            "| CPU 峰值 |  |",
            "| 内存峰值 |  |",
            "| 真实查询数 |  |",
            "| Top1 / Top5 / Top10 |  /  /  |",
            "| 需要改写查询数 |  |",
            "| Blocker / Major / Minor |  /  /  |",
            "| 备注 |  |",
            "",
            "## 隐私边界",
            "",
            "本快照仅保存聚合运行信息，不包含索引目录路径、文件路径/文件名、文档正文、问题详情或搜索历史。外发前仍应按所在单位的数据处理规范复核。",
            "",
        ]
    )
    return "\n".join(lines)


def write_beta_snapshot(
    database: SearchDatabase,
    output_dir: str | Path,
    *,
    generated_at: datetime | None = None,
) -> tuple[Path, Path]:
    snapshot = build_beta_snapshot(database, generated_at=generated_at)
    stamp = snapshot["generated_at_utc"].replace("-", "").replace(":", "")
    stamp = stamp.replace("T", "-").replace("Z", "")
    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    json_path = directory / f"beta-snapshot-{stamp}.json"
    markdown_path = directory / f"beta-snapshot-{stamp}.md"
    json_path.write_text(beta_snapshot_json(snapshot), encoding="utf-8")
    markdown_path.write_text(beta_snapshot_markdown(snapshot), encoding="utf-8")
    return json_path, markdown_path
