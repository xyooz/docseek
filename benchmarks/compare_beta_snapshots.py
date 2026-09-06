from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docseek.beta_snapshot_compare import (
    BetaSnapshotComparisonError,
    write_beta_comparison,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(
        description="Compare two privacy-safe DocSeek Beta snapshot JSON files"
    )
    parser.add_argument("before", type=Path, help="Earlier beta-snapshot JSON")
    parser.add_argument("after", type=Path, help="Later beta-snapshot JSON")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for comparison JSON/Markdown (default: directory of after snapshot)",
    )
    args = parser.parse_args()

    before = args.before.expanduser()
    after = args.after.expanduser()
    output_dir = args.output_dir.expanduser() if args.output_dir else after.parent

    try:
        json_path, markdown_path = write_beta_comparison(before, after, output_dir)
    except BetaSnapshotComparisonError as exc:
        print(f"Beta snapshot comparison failed: {exc}", file=sys.stderr)
        return 2

    print("DocSeek Beta snapshot comparison created")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print("The comparison does not include snapshot paths, database paths or document identifiers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
