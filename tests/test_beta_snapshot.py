from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from docseek.beta_snapshot import (
    beta_snapshot_json,
    beta_snapshot_markdown,
    build_beta_snapshot,
    write_beta_snapshot,
)
from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase


class BetaSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.db_path = self.base / "docseek.db"
        self.database = SearchDatabase(self.db_path)
        self.store = ChunkStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_sensitive_fixture(self) -> None:
        secret_root = self.base / "SECRET-ROOT-CUSTOMER-A"
        self.database.add_index_root(str(secret_root))
        secret_path = str(secret_root / "SECRET-FILENAME-credit-list.docx")
        self.store.replace_document(
            path=secret_path,
            filename="SECRET-FILENAME-credit-list.docx",
            extension=".docx",
            modified_time=1.0,
            size=123,
            chunks=[
                DocumentChunk(
                    0,
                    "标题 SECRET-HEADING",
                    "SECRET-DOCUMENT-TEXT customer account 123456",
                )
            ],
        )

    def test_snapshot_keeps_document_identifiers_out_of_json_and_markdown(self) -> None:
        self._seed_sensitive_fixture()
        generated_at = datetime(2026, 9, 6, 4, 45, tzinfo=timezone.utc)
        snapshot = build_beta_snapshot(self.database, generated_at=generated_at)

        json_text = beta_snapshot_json(snapshot)
        markdown = beta_snapshot_markdown(snapshot)
        combined = json_text + "\n" + markdown

        for secret in (
            "SECRET-ROOT-CUSTOMER-A",
            "SECRET-FILENAME-credit-list.docx",
            "SECRET-HEADING",
            "SECRET-DOCUMENT-TEXT",
            "123456",
            str(self.db_path),
        ):
            self.assertNotIn(secret, combined)

        self.assertEqual(snapshot["diagnostic"]["index"]["indexed_files"], 1)
        self.assertEqual(snapshot["snapshot_version"], 1)
        self.assertFalse(snapshot["privacy"]["contains_document_text"])
        self.assertFalse(snapshot["privacy"]["contains_file_paths"])
        self.assertFalse(snapshot["privacy"]["contains_database_path"])

    def test_write_snapshot_creates_parseable_json_and_fillable_markdown(self) -> None:
        self._seed_sensitive_fixture()
        output_dir = self.base / "reports"
        generated_at = datetime(2026, 9, 6, 4, 45, 12, tzinfo=timezone.utc)

        json_path, markdown_path = write_beta_snapshot(
            self.database,
            output_dir,
            generated_at=generated_at,
        )

        self.assertEqual(json_path.name, "beta-snapshot-20260906-044512.json")
        self.assertEqual(markdown_path.name, "beta-snapshot-20260906-044512.md")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(payload["diagnostic"]["index"]["indexed_files"], 1)
        sqlite_files = payload["sqlite_files"]
        self.assertEqual(
            sqlite_files["total_bytes"],
            sqlite_files["database_bytes"]
            + sqlite_files["wal_bytes"]
            + sqlite_files["shm_bytes"],
        )
        self.assertIn("## 人工补录", markdown)
        self.assertIn("Top1 / Top5 / Top10", markdown)
        self.assertIn("Blocker / Major / Minor", markdown)


if __name__ == "__main__":
    unittest.main()
