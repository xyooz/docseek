from __future__ import annotations

import os


def main() -> int:
    # Used by the packaging workflow to prove the frozen executable can import
    # DocSeek and all production entry-point modules without opening a window.
    if os.environ.get("DOCSEEK_FROZEN_SMOKE") == "1":
        import docseek.app  # noqa: F401
        import docseek.bootstrap  # noqa: F401
        import docseek.document_adapters  # noqa: F401
        import docseek.indexer  # noqa: F401
        import docseek.index_maintenance  # noqa: F401

        return 0

    from docseek.bootstrap import main as run_docseek

    run_docseek()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
