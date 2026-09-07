from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index_health import format_storage_size


BETA_COMPARISON_VERSION = 1


class BetaSnapshotComparisonError(ValueError):
    """Raised when a Beta snapshot cannot be compared safely."""


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BetaSnapshotComparisonError(f"快照缺少有效字段：{name}")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BetaSnapshotComparisonError(f"快照字段不是整数：{name}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BetaSnapshotComparisonError(f"快照字段不是整数：{name}") from exc


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_beta_snapshot(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BetaSnapshotComparisonError(f"无法读取 Beta 快照：{exc}") from exc
    if not isinstance(payload, dict):
        raise BetaSnapshotComparisonError("Beta 快照顶层必须是 JSON 对象。")
    _integer(payload.get("snapshot_version"), "snapshot_version")
    _mapping(payload.get("diagnostic"), "diagnostic")
    _mapping(payload.get("sqlite_files"), "sqlite_files")
    return payload


def _snapshot_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    diagnostic = _mapping(snapshot.get("diagnostic"), "diagnostic")
    docseek = _mapping(diagnostic.get("docseek"), "diagnostic.docseek")
    index = _mapping(diagnostic.get("index"), "diagnostic.index")
    roots = _mapping(index.get("roots"), "diagnostic.index.roots")
    issues = _mapping(index.get("issues"), "diagnostic.index.issues")
    sqlite_files = _mapping(snapshot.get("sqlite_files"), "sqlite_files")
    issue_codes_raw = _mapping(
        issues.get("by_error_code"),
        "diagnostic.index.issues.by_error_code",
    )
    issue_codes = {
        str(code): _integer(count, f"issue code {code}")
        for code, count in issue_codes_raw.items()
    }
    return {
        "snapshot_version": _integer(snapshot.get("snapshot_version"), "snapshot_version"),
        "generated_at_utc": _optional_text(snapshot.get("generated_at_utc")),
        "docseek_version": _optional_text(docseek.get("version")),
        "schema_actual": _integer(docseek.get("schema_actual"), "schema_actual"),
        "schema_expected": _integer(docseek.get("schema_expected"), "schema_expected"),
        "indexed_files": _integer(index.get("indexed_files"), "indexed_files"),
        "issues_total": _integer(issues.get("total"), "issues.total"),
        "issue_codes": issue_codes,
        "roots_total": _integer(roots.get("total"), "roots.total"),
        "roots_active": _integer(roots.get("active"), "roots.active"),
        "roots_paused": _integer(roots.get("paused"), "roots.paused"),
        "last_reconcile": _optional_text(index.get("last_successful_reconcile_at_utc")),
        "database_bytes": _integer(sqlite_files.get("database_bytes"), "database_bytes"),
        "wal_bytes": _integer(sqlite_files.get("wal_bytes"), "wal_bytes"),
        "shm_bytes": _integer(sqlite_files.get("shm_bytes"), "shm_bytes"),
        "total_bytes": _integer(sqlite_files.get("total_bytes"), "total_bytes"),
    }


def compare_beta_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Compare two privacy-safe Beta snapshots without touching source documents."""

    left = _snapshot_view(before)
    right = _snapshot_view(after)

    before_time = _parse_utc(left["generated_at_utc"])
    after_time = _parse_utc(right["generated_at_utc"])
    elapsed_seconds: float | None = None
    if before_time is not None and after_time is not None:
        elapsed_seconds = (after_time - before_time).total_seconds()

    issue_code_delta: dict[str, int] = {}
    for code in sorted(set(left["issue_codes"]) | set(right["issue_codes"])):
        delta = right["issue_codes"].get(code, 0) - left["issue_codes"].get(code, 0)
        if delta:
            issue_code_delta[code] = delta

    indexed_delta = right["indexed_files"] - left["indexed_files"]
    issues_delta = right["issues_total"] - left["issues_total"]
    database_delta = right["database_bytes"] - left["database_bytes"]
    wal_delta = right["wal_bytes"] - left["wal_bytes"]
    shm_delta = right["shm_bytes"] - left["shm_bytes"]
    total_delta = right["total_bytes"] - left["total_bytes"]

    attention: list[str] = []
    if right["schema_actual"] != right["schema_expected"]:
        attention.append("结束快照的 Schema 与当前程序期望值不一致。")
    if issues_delta > 0:
        attention.append(f"持久化索引问题增加 {issues_delta} 个，需要查看错误代码汇总。")
    if indexed_delta < 0:
        attention.append(
            f"已索引文件减少 {-indexed_delta} 个；若期间没有主动删除、移动或排除文件，需要复核最终一致性。"
        )
    if elapsed_seconds is not None and elapsed_seconds < 0:
        attention.append("结束快照时间早于开始快照，请检查快照顺序或系统时间。")

    return {
        "comparison_version": BETA_COMPARISON_VERSION,
        "before_generated_at_utc": left["generated_at_utc"],
        "after_generated_at_utc": right["generated_at_utc"],
        "elapsed_seconds": elapsed_seconds,
        "compatibility": {
            "snapshot_version_changed": left["snapshot_version"] != right["snapshot_version"],
            "docseek_version_changed": left["docseek_version"] != right["docseek_version"],
            "schema_changed": left["schema_actual"] != right["schema_actual"],
            "before_docseek_version": left["docseek_version"],
            "after_docseek_version": right["docseek_version"],
            "before_schema": left["schema_actual"],
            "after_schema": right["schema_actual"],
        },
        "index": {
            "indexed_files": {
                "before": left["indexed_files"],
                "after": right["indexed_files"],
                "delta": indexed_delta,
            },
            "issues": {
                "before": left["issues_total"],
                "after": right["issues_total"],
                "delta": issues_delta,
                "by_error_code_delta": issue_code_delta,
            },
            "roots": {
                "total_delta": right["roots_total"] - left["roots_total"],
                "active_delta": right["roots_active"] - left["roots_active"],
                "paused_delta": right["roots_paused"] - left["roots_paused"],
            },
            "last_successful_reconcile": {
                "before": left["last_reconcile"],
                "after": right["last_reconcile"],
                "changed": left["last_reconcile"] != right["last_reconcile"],
            },
        },
        "sqlite_files": {
            "database_bytes": {
                "before": left["database_bytes"],
                "after": right["database_bytes"],
                "delta": database_delta,
            },
            "wal_bytes": {
                "before": left["wal_bytes"],
                "after": right["wal_bytes"],
                "delta": wal_delta,
            },
            "shm_bytes": {
                "before": left["shm_bytes"],
                "after": right["shm_bytes"],
                "delta": shm_delta,
            },
            "total_bytes": {
                "before": left["total_bytes"],
                "after": right["total_bytes"],
                "delta": total_delta,
            },
        },
        "attention": attention,
        "privacy": {
            "contains_snapshot_paths": False,
            "contains_database_path": False,
            "contains_document_text": False,
            "contains_filenames": False,
            "contains_file_paths": False,
            "contains_index_root_paths": False,
        },
    }


def beta_comparison_json(comparison: dict[str, Any]) -> str:
    return json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _signed(value: int) -> str:
    return f"{value:+d}"


def _size_delta(value: int) -> str:
    if value == 0:
        return "0 B"
    sign = "+" if value > 0 else "-"
    return sign + format_storage_size(abs(value))


def beta_comparison_markdown(comparison: dict[str, Any]) -> str:
    compatibility = comparison["compatibility"]
    index = comparison["index"]
    sqlite_files = comparison["sqlite_files"]
    elapsed = comparison.get("elapsed_seconds")
    elapsed_text = "未知" if elapsed is None else f"{elapsed:.0f} 秒"

    lines = [
        "# DocSeek Beta 快照对比",
        "",
        f"开始快照（UTC）：{comparison.get('before_generated_at_utc') or '未知'}",
        f"结束快照（UTC）：{comparison.get('after_generated_at_utc') or '未知'}",
        f"间隔：{elapsed_text}",
        "",
        "## 主要变化",
        "",
        "| 指标 | 开始 | 结束 | 变化 |",
        "| --- | ---: | ---: | ---: |",
        f"| 已索引文件 | {index['indexed_files']['before']} | {index['indexed_files']['after']} | {_signed(index['indexed_files']['delta'])} |",
        f"| 索引问题 | {index['issues']['before']} | {index['issues']['after']} | {_signed(index['issues']['delta'])} |",
        f"| DB | {format_storage_size(sqlite_files['database_bytes']['before'])} | {format_storage_size(sqlite_files['database_bytes']['after'])} | {_size_delta(sqlite_files['database_bytes']['delta'])} |",
        f"| WAL | {format_storage_size(sqlite_files['wal_bytes']['before'])} | {format_storage_size(sqlite_files['wal_bytes']['after'])} | {_size_delta(sqlite_files['wal_bytes']['delta'])} |",
        f"| SHM | {format_storage_size(sqlite_files['shm_bytes']['before'])} | {format_storage_size(sqlite_files['shm_bytes']['after'])} | {_size_delta(sqlite_files['shm_bytes']['delta'])} |",
        f"| SQLite 总占用 | {format_storage_size(sqlite_files['total_bytes']['before'])} | {format_storage_size(sqlite_files['total_bytes']['after'])} | {_size_delta(sqlite_files['total_bytes']['delta'])} |",
        "",
        "## 兼容性与校准",
        "",
        f"- DocSeek：{compatibility['before_docseek_version']} → {compatibility['after_docseek_version']}",
        f"- Schema：{compatibility['before_schema']} → {compatibility['after_schema']}",
        f"- 最近完整校准：{index['last_successful_reconcile']['before'] or '无'} → {index['last_successful_reconcile']['after'] or '无'}",
        "",
        "## 索引问题代码变化",
        "",
    ]

    issue_delta = index["issues"]["by_error_code_delta"]
    if issue_delta:
        lines.extend(["| 错误代码 | 变化 |", "| --- | ---: |"])
        for code, delta in sorted(issue_delta.items()):
            lines.append(f"| `{code}` | {_signed(int(delta))} |")
    else:
        lines.append("错误代码汇总没有变化。")

    lines.extend(["", "## 需要关注", ""])
    if comparison["attention"]:
        lines.extend(f"- {item}" for item in comparison["attention"])
    else:
        lines.append("未发现基于聚合指标即可判定的异常变化。")

    lines.extend(
        [
            "",
            "> 注意：数据库/WAL 增长、文件数减少等变化是否正常，仍需结合测试期间是否新增、删除、移动或重新索引文件判断。该报告不会自动把所有增长判定为故障。",
            "",
            "本对比只处理两份已脱敏的 Beta Snapshot JSON，不包含快照本地路径、数据库路径、索引目录、文件名或正文。",
            "",
        ]
    )
    return "\n".join(lines)


def write_beta_comparison(
    before_path: str | Path,
    after_path: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    before = load_beta_snapshot(before_path)
    after = load_beta_snapshot(after_path)
    comparison = compare_beta_snapshots(before, after)

    after_time = _parse_utc(comparison.get("after_generated_at_utc"))
    if after_time is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    else:
        stamp = after_time.strftime("%Y%m%d-%H%M%S")

    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"beta-comparison-{stamp}.json"
    markdown_path = directory / f"beta-comparison-{stamp}.md"
    json_path.write_text(beta_comparison_json(comparison), encoding="utf-8")
    markdown_path.write_text(beta_comparison_markdown(comparison), encoding="utf-8")
    return json_path, markdown_path
