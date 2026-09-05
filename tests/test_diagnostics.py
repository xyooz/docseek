from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from docseek.diagnostics import build_diagnostic_report, write_diagnostic_report
from docseek.file_exclusions import FileExclusionStore
from docseek.index_issues import IndexIssueStore
from docseek.index_root_state import IndexRootStateStore
from docseek.schema import CURRENT_SCHEMA_VERSION
from docseek.search_db import SearchDatabase


class DiagnosticReportTests(unittest.TestCase):
    def test_report_is_aggregate_only_and_omits_sensitive_paths_and_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_path = base / "docseek.db"
            db = SearchDatabase(db_path)

            root = base / "SECRET_ROOT_CUSTOMER_FILES"
            excluded = root / "SECRET_EXCLUDED_DIRECTORY"
            root.mkdir()
            excluded.mkdir()
            db.add_index_root(str(root))
            db.add_excluded_path(str(excluded))
            IndexRootStateStore(db).set_paused(root, True)
            FileExclusionStore(db).set_patterns(["SECRET_PATTERN_*.docx"])
            db.set_max_file_size_mb(321)

            issue_path = root / "SECRET_FILE_NAME.pdf"
            IndexIssueStore(db_path).record(
                str(issue_path.resolve()),
                "permission_denied",
                "SECRET_DETAIL_FROM_OS",
            )

            report = build_diagnostic_report(
                db,
                generated_at=datetime(2026, 9, 5, 15, 30, tzinfo=timezone.utc),
            )
            encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertEqual(report["report_version"], 1)
            self.assertEqual(report["generated_at_utc"], "2026-09-05T15:30:00Z")
            self.assertEqual(report["docseek"]["schema_expected"], CURRENT_SCHEMA_VERSION)
            self.assertEqual(report["docseek"]["schema_actual"], CURRENT_SCHEMA_VERSION)
            self.assertEqual(report["index"]["roots"], {"total": 1, "active": 0, "paused": 1})
            self.assertEqual(report["index"]["issues"]["total"], 1)
            self.assertEqual(
                report["index"]["issues"]["by_error_code"],
                {"permission_denied": 1},
            )
            self.assertEqual(report["index"]["excluded_directory_count"], 1)
            self.assertEqual(report["index"]["excluded_file_pattern_count"], 1)
            self.assertEqual(report["index"]["max_file_size_mb"], 321)

            for secret in (
                "SECRET_ROOT_CUSTOMER_FILES",
                "SECRET_EXCLUDED_DIRECTORY",
                "SECRET_PATTERN_",
                "SECRET_FILE_NAME",
                "SECRET_DETAIL_FROM_OS",
                str(root.resolve()),
                str(excluded.resolve()),
                str(issue_path.resolve()),
            ):
                self.assertNotIn(secret, encoded)

            self.assertTrue(all(value is False for value in report["privacy"].values()))

    def test_write_report_creates_utf8_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db = SearchDatabase(base / "docseek.db")
            destination = base / "support" / "diagnostics.json"

            written = write_diagnostic_report(db, destination)

            self.assertEqual(written, destination)
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["report_version"], 1)
            self.assertIn("runtime", payload)
            self.assertFalse(payload["privacy"]["contains_document_text"])


if __name__ == "__main__":
    unittest.main()
