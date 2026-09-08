import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from docseek.search_db import SearchDatabase
from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.indexer import DirectoryIndexer, IndexCancelled
from docseek.search_worker import SearchWorker, SearchRequest
from docseek.search_session import close_thread_search_store
from docseek.settings_dialog import IndexSettingsDialog


class ResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_custom_stays_open_when_selection_matches_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            dialog = IndexSettingsDialog(SearchDatabase(Path(tmp) / "index.db"))
            combo = dialog.format_preset_combo
            combo.setCurrentIndex(combo.findData("custom"))
            box = dialog.format_checkboxes[".xml"]
            box.setChecked(True)
            box.setChecked(False)
            self.assertEqual(combo.currentData(), "custom")
            self.assertFalse(dialog.format_scroll.isHidden())
            dialog.close()

    def test_family_filter_includes_old_and_wps_formats_and_exact_stays_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = SearchDatabase(Path(tmp) / "index.db")
            store = ChunkStore(db.db_path)
            for ext in (".doc", ".docx", ".wps", ".et"):
                store.replace_document(
                    path=str(Path(tmp) / ("file" + ext)), filename="file" + ext,
                    extension=ext, modified_time=1, size=1,
                    chunks=[DocumentChunk(0, "", "共同内容")],
                )
            engine = ExactGroupedSearchEngine(store)
            for query in ("", "共同内容"):
                rows = engine.search_page(query, extension="@writer").items
                self.assertEqual({r.extension for r in rows}, {".doc", ".docx", ".wps"})
                rows = engine.search_page(query, extension=".docx").items
                self.assertEqual([r.extension for r in rows], [".docx"])

    def test_running_query_cancelled_and_next_query_can_use_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = SearchDatabase(Path(tmp) / "index.db")
            store = ChunkStore(db.db_path)
            old = SearchWorker(store, SearchRequest(1, "old", 10, 0))
            responses, errors = [], []
            old.signals.finished.connect(responses.append)
            old.signals.failed.connect(lambda *args: errors.append(args))
            class SlowEngine:
                def __init__(self, session):
                    self.session = session
                def search_page(self, *args, **kwargs):
                    SearchWorker(store, SearchRequest(2, "new", 10, 0))
                    with self.session.connect() as conn:
                        conn.execute(
                            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
                            "SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n"
                        ).fetchone()
            try:
                with patch("docseek.search_worker.ExactGroupedSearchEngine", SlowEngine):
                    old.run()
                self.assertEqual(errors, [])
                self.assertEqual(len(responses), 1)
                new = SearchWorker(store, SearchRequest(3, "new", 10, 0))
                new.signals.failed.connect(lambda *args: errors.append(args))
                new.run()
                self.assertEqual(errors, [])
            finally:
                close_thread_search_store()

    def test_indexing_starts_before_discovery_ends_and_cancel_keeps_old_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            root.mkdir()
            for n in range(260):
                (root / f"{n}.txt").write_text("共同内容", encoding="utf-8")
            db = SearchDatabase(Path(tmp) / "index.db")
            indexer = DirectoryIndexer(db)
            discovered, first_index = [], []
            original = indexer._iter_supported_files
            def walking(*args, **kwargs):
                for path in original(*args, **kwargs):
                    discovered.append(path)
                    yield path
            def progress(path, stats):
                if stats.indexed and not first_index:
                    first_index.append(len(discovered))
            with patch.object(indexer, "_iter_supported_files", walking):
                stats = indexer.scan(root, on_progress=progress)
            self.assertEqual(stats.indexed, 260)
            self.assertLess(first_index[0], 260)
            expected = set(indexer._load_index_state(root))
            def stop(path, stats):
                indexer.cancel()
            with self.assertRaises(IndexCancelled):
                indexer.scan(root, on_discovery=stop)
            self.assertEqual(set(indexer._load_index_state(root)), expected)

    def test_state_scope_does_not_include_sibling_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = SearchDatabase(Path(tmp) / "index.db")
            indexer = DirectoryIndexer(db)
            for name in ("docs", "docs-backup"):
                directory = Path(tmp) / name
                directory.mkdir()
                (directory / "file.txt").write_text("内容", encoding="utf-8")
                indexer.scan(directory)
            state = indexer._load_index_state(Path(tmp) / "docs")
            self.assertEqual(len(state), 1)
            self.assertNotIn("docs-backup", next(iter(state)))
