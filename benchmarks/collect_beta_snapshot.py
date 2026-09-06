from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docseek.beta_snapshot import write_beta_snapshot
from docseek.search_db import SearchDatabase


DEFAULT_DB_PATH = Path.home() / ".docseek" / "docseek.db"
DEFAULT_OUTPUT_DIR = Path.home() / ".docseek" / "beta-reports"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(
        description="Collect a privacy-safe DocSeek Windows Beta validation snapshot"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="DocSeek database path (default: ~/.docseek/docseek.db)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for JSON and Markdown snapshots",
    )
    args = parser.parse_args()

    db_path = args.db.expanduser()
    if not db_path.exists() or not db_path.is_file():
        print(
            "DocSeek index database not found. Start DocSeek and build an index first, "
            "or pass --db explicitly.",
            file=sys.stderr,
        )
        return 2

    database = SearchDatabase(db_path)
    json_path, markdown_path = write_beta_snapshot(database, args.output_dir)
    print("DocSeek Beta snapshot created")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print("The report excludes document text, filenames, file paths and index-root paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
