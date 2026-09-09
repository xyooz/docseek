from __future__ import annotations

import os
import sys


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

    run_docseek()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
