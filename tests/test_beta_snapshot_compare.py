from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docseek.beta_snapshot_compare import (
    BetaSnapshotComparisonError,
    beta_comparison_json,
    beta_comparison_markdown,
    compare_beta_snapshots,
    load_beta_snapshot,
    write_beta_comparison,
)


def snapshot(
    *,
    generated_at: str,
    indexed_files: int = 100,
    issues: int = 0,
    issue_codes: dict[str, int] | None = None,
    db_bytes: int = 1000,
    wal_bytes: int = 0,
    shm_bytes: int = 0,
    reconcile: str | None = "2026-09-06T04:00:00Z",
    version: str = "0.2.0",
    schema: int = 10,
) -> dict:
    return {
        "snapshot_version": 1,
        "generated_at_utc": generated_at,
        "diagnostic": {
            "docseek": {
                "version": version,
                "schema_actual": schema,
                "schema_expected": 10,
            },
            "index": {
                "indexed_files": indexed_files,
                "roots": {"total": 1, "active": 1, "paused": 0},
                "issues": {
                    "total": issues,
                    "by_error_code": issue_codes or {},
                },
                "last_successful_reconcile_at_utc": reconcile,
            },
        },
        "sqlite_files": {
            "database_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "shm_bytes": shm_bytes,
            "total_bytes": db_bytes + wal_bytes + shm_bytes,
        },
    }


class BetaSnapshotComparisonTests(unittest.TestCase):
    def test_comparison_reports_deltas_and_attention_without_guessing_wal_failure(self) -> None:
        before = snapshot(
            generated_at="2026-09-06T01:00:00Z",
            indexed_files=100,
            issues=1,
            issue_codes={"parse_failed": 1},
            wal_bytes=1024,
        )
        after = snapshot(
            generated_at="2026-09-06T05:00:00Z",
            indexed_files=98,
            issues=3,
            issue_codes={"parse_failed": 2, "timeout": 1},
            db_bytes=2048,
            wal_bytes=50 * 1024 * 1024,
            reconcile="2026-09-06T04:59:00Z",
        )

        result = compare_beta_snapshots(before, after)

        self.assertEqual(result["elapsed_seconds"], 4 * 3600)
        self.assertEqual(result["index"]["indexed_files"]["delta"], -2)
        self.assertEqual(result["index"]["issues"]["delta"], 2)
        self.assertEqual(
            result["index"]["issues"]["by_error_code_delta"],
            {"parse_failed": 1, "timeout": 1},
        )
        self.assertGreater(result["sqlite_files"]["wal_bytes"]["delta"], 0)
        self.assertTrue(any("索引问题增加 2" in item for item in result["attention"]))
        self.assertTrue(any("已索引文件减少 2" in item for item in result["attention"]))
        self.assertFalse(any("WAL" in item for item in result["attention"]))

    def test_plain_growth_is_reported_but_not_automatically_declared_abnormal(self) -> None:
        before = snapshot(generated_at="2026-09-06T01:00:00Z", wal_bytes=0)
        after = snapshot(
            generated_at="2026-09-06T02:00:00Z",
            indexed_files=100,
            issues=0,
            wal_bytes=128 * 1024 * 1024,
        )
        result = compare_beta_snapshots(before, after)
        self.assertEqual(result["attention"], [])
        markdown = beta_comparison_markdown(result)
        self.assertIn("WAL", markdown)
        self.assertIn("不会自动把所有增长判定为故障", markdown)

    def test_written_comparison_does_not_embed_input_snapshot_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            before_path = base / "SECRET-BEFORE-PATH.json"
            after_path = base / "SECRET-AFTER-PATH.json"
            before_path.write_text(
                json.dumps(snapshot(generated_at="2026-09-06T01:00:00Z")),
                encoding="utf-8",
            )
            after_path.write_text(
                json.dumps(snapshot(generated_at="2026-09-06T02:00:00Z")),
                encoding="utf-8",
            )

            json_path, markdown_path = write_beta_comparison(
                before_path,
                after_path,
                base / "reports",
            )
            combined = json_path.read_text(encoding="utf-8") + markdown_path.read_text(
                encoding="utf-8"
            )
            self.assertNotIn("SECRET-BEFORE-PATH", combined)
            self.assertNotIn("SECRET-AFTER-PATH", combined)
            self.assertNotIn(str(base), combined)
            self.assertIn("contains_snapshot_paths", combined)

    def test_invalid_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.json"
            path.write_text('{"snapshot_version": 1}', encoding="utf-8")
            with self.assertRaises(BetaSnapshotComparisonError):
                load_beta_snapshot(path)


if __name__ == "__main__":
    unittest.main()
