from __future__ import annotations

import os
import sys


def _hide_owned_windows_console() -> None:
    """Hide the console created for the desktop executable, if it owns one."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return
        # Do not hide a console that belongs to a developer's existing shell.
        process_ids = (ctypes.c_uint32 * 16)()
        count = kernel32.GetConsoleProcessList(process_ids, len(process_ids))
        if count == 1:
            user32.ShowWindow(hwnd, 0)
    except Exception:
        # Console hiding is cosmetic; startup must remain usable if a Windows
        # API is unavailable on an unusual host.
        return


def _run_frozen_rust_smoke() -> int:
    """Run a real strict-Rust index/search check inside the frozen app."""
    import tempfile
    from pathlib import Path

    import docseek_rust  # type: ignore[import-not-found]

    from docseek.chunk_store import ChunkStore
    from docseek.indexer import DirectoryIndexer
    from docseek.scan_backend import RustScanBackend, resolve_scan_backend
    from docseek.search_db import SearchDatabase

    auto_backend = resolve_scan_backend("auto")
    if getattr(auto_backend, "scan_backend_name", None) != "rust":
        raise RuntimeError("frozen auto backend did not select Rust")

    with tempfile.TemporaryDirectory(prefix="docseek-frozen-rust-") as temp_dir:
        base = Path(temp_dir)
        root = base / "documents"
        root.mkdir()
        (root / "marker.txt").write_text(
            "docseek frozen rust marker\n",
            encoding="utf-8",
        )

        database = SearchDatabase(base / "docseek.db")
        indexer = DirectoryIndexer(
            database,
            enabled_extensions={".txt"},
            scan_backend=RustScanBackend(
                rust_module=docseek_rust,
                allow_fallback=False,
            ),
        )
        stats = indexer.scan(root)
        if indexer.scan_backend_name != "rust":
            raise RuntimeError(
                f"strict frozen smoke used {indexer.scan_backend_name!r}"
            )
        if stats.indexed != 1:
            raise RuntimeError(
                f"strict frozen smoke indexed {stats.indexed} files, expected 1"
            )

        matches = ChunkStore(database.db_path).search("frozen rust marker")
        if not matches:
            raise RuntimeError("strict frozen smoke search did not find its marker")

    return 0


def _run_frozen_index_smoke() -> int:
    """Run a real frozen full-index smoke with persistent extraction enabled.

    The parent verifier supplies the corpus and report paths through the
    environment.  Keeping this check in the frozen launcher makes the test
    exercise the same DirectoryIndexer/ExtractionBroker path as the shipped
    application, while the small recording subclass exposes only test
    diagnostics (worker reuse and cleanup).
    """
    import json
    from pathlib import Path

    import docseek_rust  # type: ignore[import-not-found]

    from docseek import indexer as indexer_module
    from docseek.chunk_store import ChunkStore
    from docseek.indexer import DirectoryIndexer
    from docseek.persistent_extraction import PersistentExtractionWorker
    from docseek.scan_backend import RustScanBackend
    from docseek.search_db import SearchDatabase

    root = Path(os.environ["DOCSEEK_FROZEN_INDEX_ROOT"]).resolve()
    database_path = Path(os.environ["DOCSEEK_FROZEN_INDEX_DB"]).resolve()
    report_path = Path(os.environ["DOCSEEK_FROZEN_INDEX_REPORT"]).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)

    worker_instances: list[PersistentExtractionWorker] = []

    class RecordingWorker(PersistentExtractionWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.request_pids: list[int | None] = []
            worker_instances.append(self)

        def extract(self, *args, **kwargs):
            process = self._process
            self.request_pids.append(None if process is None else process.pid)
            return super().extract(*args, **kwargs)

    original_worker = indexer_module.PersistentExtractionWorker
    indexer_module.PersistentExtractionWorker = RecordingWorker
    report: dict[str, object] = {"ok": False, "error": "smoke did not run"}
    try:
        database = SearchDatabase(database_path)
        database.add_index_root(str(root))
        indexer = DirectoryIndexer(
            database,
            scan_backend=RustScanBackend(
                rust_module=docseek_rust,
                allow_fallback=False,
            ),
        )
        stats = indexer.scan(root)
        worker = worker_instances[0] if worker_instances else None
        pids = [] if worker is None else list(worker.request_pids)
        unique_pids = sorted({pid for pid in pids if pid is not None})

        expected_queries = {
            "docx": "portable persistent DOCX marker",
            "xlsx": "冻结 worker XLSX marker",
            "pptx": "portable persistent PPTX marker",
            "pdf": "portable persistent PDF marker",
        }
        search_hits = {
            name: bool(ChunkStore(database_path).search(query))
            for name, query in expected_queries.items()
        }

        if indexer.scan_backend_name != "rust":
            raise RuntimeError(
                f"frozen production smoke used {indexer.scan_backend_name!r}"
            )
        if stats.indexed != 4:
            raise RuntimeError(
                f"frozen production smoke indexed {stats.indexed} files, expected 4"
            )
        if not all(search_hits.values()):
            raise RuntimeError(f"frozen production search misses: {search_hits!r}")
        if (
            worker is None
            or len(pids) < 4
            or any(pid is None for pid in pids)
            or len(unique_pids) != 1
        ):
            raise RuntimeError(
                "frozen production smoke did not reuse one persistent worker: "
                f"request_pids={pids!r}"
            )
        if worker.state != "closed" or worker.is_alive:
            raise RuntimeError(
                "frozen production smoke left the persistent worker alive: "
                f"state={worker.state!r} alive={worker.is_alive!r}"
            )

        report = {
            "ok": True,
            "scan_backend": indexer.scan_backend_name,
            "stats": stats.__dict__ if hasattr(stats, "__dict__") else {
                field: getattr(stats, field)
                for field in (
                    "scanned",
                    "indexed",
                    "chunks",
                    "unchanged",
                    "skipped",
                    "removed",
                    "excluded",
                    "no_text",
                    "ocr_required",
                )
            },
            "request_pids": pids,
            "worker_pid": unique_pids[0],
            "worker_state": worker.state,
            "worker_alive": worker.is_alive,
            "search_hits": search_hits,
        }
    except Exception as exc:
        report = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "request_pids": (
                []
                if not worker_instances
                else list(getattr(worker_instances[0], "request_pids", []))
            ),
        }
        raise
    finally:
        indexer_module.PersistentExtractionWorker = original_worker
        report_path.write_text(
            json.dumps(report, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--docseek-preview-worker":
        from docseek.location_preview import main as run_preview_worker

        return run_preview_worker(sys.argv[2:])

    # Hidden subprocess entrypoint used by Index Pipeline V2. It must run before
    # the normal bootstrap/single-instance lock because parser workers are child
    # processes of the already-running DocSeek desktop instance.
    if len(sys.argv) >= 2 and sys.argv[1] == "--docseek-extract-worker":
        from docseek.legacy_worker import main as run_legacy_worker

        return run_legacy_worker(sys.argv[2:])

    if os.environ.get("DOCSEEK_FROZEN_RUST_SMOKE") == "1":
        return _run_frozen_rust_smoke()

    if os.environ.get("DOCSEEK_FROZEN_INDEX_SMOKE") == "1":
        return _run_frozen_index_smoke()

    # Used by the packaging workflow to prove the frozen executable can import
    # DocSeek and all production entry-point modules without opening a window.
    if os.environ.get("DOCSEEK_FROZEN_SMOKE") == "1":
        import docseek.app  # noqa: F401
        import docseek.bootstrap  # noqa: F401
        import docseek.document_adapters  # noqa: F401
        import docseek.indexer  # noqa: F401
        import docseek.index_maintenance  # noqa: F401
        import docseek.legacy_isolation  # noqa: F401
        import docseek.legacy_worker  # noqa: F401
        import docseek.persistent_extraction  # noqa: F401

        return 0

    from docseek.bootstrap import main as run_docseek

    _hide_owned_windows_console()
    run_docseek()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
