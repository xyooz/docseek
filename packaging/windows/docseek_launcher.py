from __future__ import annotations

import os
import sys


def main() -> int:
    # Hidden subprocess entrypoint used by Index Pipeline V2. It must run before
    # the normal bootstrap/single-instance lock because parser workers are child
    # processes of the already-running DocSeek desktop instance.
    if len(sys.argv) >= 2 and sys.argv[1] == "--docseek-extract-worker":
        from docseek.legacy_worker import main as run_legacy_worker

        return run_legacy_worker(sys.argv[2:])

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

        return 0

    from docseek.bootstrap import main as run_docseek

    run_docseek()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
