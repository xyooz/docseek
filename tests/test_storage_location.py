from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.search_db import SearchDatabase
from docseek.storage_location import (
    DATABASE_FILENAME,
    StorageLocationError,
    apply_pending_index_storage_move,
    configured_database_path,
    configured_index_data_dir,
    save_index_data_dir,
    stage_index_storage_move,
)


class StorageLocationTests(unittest.TestCase):
    def test_missing_config_uses_legacy_default_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config = base / "control" / "storage.json"
            legacy = base / "legacy"

            self.assertEqual(
                configured_index_data_dir(config_path=config, default_dir=legacy),
                legacy.resolve(),
            )
            self.assertEqual(
                configured_database_path(config_path=config, default_dir=legacy),
                legacy.resolve() / DATABASE_FILENAME,
            )

    def test_saved_location_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config = base / "control" / "storage.json"
            destination = base / "E-drive" / "DocSeekData"

            saved = save_index_data_dir(destination, config_path=config)
            self.assertEqual(saved, destination.resolve())
            self.assertEqual(
                configured_database_path(config_path=config, default_dir=base / "legacy"),
                destination.resolve() / DATABASE_FILENAME,
            )

    def test_stage_rejects_existing_database_in_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config = base / "control" / "storage.json"
            pending = base / "control" / "pending.json"
            legacy = base / "legacy"
            destination = base / "target"
            destination.mkdir()
            (destination / DATABASE_FILENAME).write_bytes(b"unknown")

            with self.assertRaises(StorageLocationError):
                stage_index_storage_move(
                    destination,
                    config_path=config,
                    pending_path=pending,
                    default_dir=legacy,
                )
            self.assertFalse(pending.exists())

    def test_apply_move_copies_valid_index_switches_config_and_removes_old_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            control = base / "control"
            config = control / "storage.json"
            pending = control / "pending.json"
            legacy = base / "legacy"
            destination = base / "target"
            old_db = legacy / DATABASE_FILENAME

            database = SearchDatabase(old_db)
            store = ChunkStore(old_db)
            source = base / "source.txt"
            source.write_text("迁移后的索引仍然可以搜索", encoding="utf-8")
            stat = source.stat()
            database.add_index_root(str(base))
            store.replace_document(
                path=str(source.resolve()),
                filename=source.name,
                extension=".txt",
                modified_time=stat.st_mtime,
                size=stat.st_size,
                chunks=[DocumentChunk(0, "文本行 1-1", "迁移后的索引仍然可以搜索")],
            )

            stage_index_storage_move(
                destination,
                config_path=config,
                pending_path=pending,
                default_dir=legacy,
            )
            result = apply_pending_index_storage_move(
                config_path=config,
                pending_path=pending,
                default_dir=legacy,
            )

            self.assertIsNotNone(result)
            assert result is not None
            new_db = destination.resolve() / DATABASE_FILENAME
            self.assertEqual(result.previous_db_path, old_db.resolve())
            self.assertEqual(result.current_db_path, new_db)
            self.assertTrue(result.moved_existing_index)
            self.assertTrue(result.old_files_removed)
            self.assertFalse(pending.exists())
            self.assertFalse(old_db.exists())
            self.assertEqual(
                configured_database_path(config_path=config, default_dir=legacy),
                new_db,
            )
            self.assertEqual(
                [row.filename for row in ChunkStore(new_db).search("迁移后的索引")],
                [source.name],
            )

    def test_config_switch_failure_keeps_old_index_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            control = base / "control"
            config = control / "storage.json"
            pending = control / "pending.json"
            legacy = base / "legacy"
            destination = base / "target"
            old_db = legacy / DATABASE_FILENAME
            SearchDatabase(old_db)

            stage_index_storage_move(
                destination,
                config_path=config,
                pending_path=pending,
                default_dir=legacy,
            )

            with patch(
                "docseek.storage_location.save_index_data_dir",
                side_effect=StorageLocationError("synthetic config failure"),
            ):
                with self.assertRaises(StorageLocationError):
                    apply_pending_index_storage_move(
                        config_path=config,
                        pending_path=pending,
                        default_dir=legacy,
                    )

            self.assertTrue(old_db.exists())
            self.assertTrue(pending.exists())
            self.assertFalse((destination / DATABASE_FILENAME).exists())
            self.assertEqual(
                configured_database_path(config_path=config, default_dir=legacy),
                old_db.resolve(),
            )


if __name__ == "__main__":
    unittest.main()
