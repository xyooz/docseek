from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path
from typing import Iterable

from .chunks import DocumentChunk
from .extraction_broker import DEFAULT_EXTRACTION_BROKER


def write_chunk_file(path: Path, chunks: Iterable[DocumentChunk]) -> int:
    """Write trusted worker output atomically for replay by the parent process."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    count = 0
    try:
        with partial.open("wb") as handle:
            for chunk in chunks:
                pickle.dump(
                    (int(chunk.ordinal), str(chunk.location), str(chunk.content)),
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
                count += 1
            handle.flush()
        partial.replace(path)
        return count
    finally:
        partial.unlink(missing_ok=True)


def iter_chunk_file(path: Path):
    with Path(path).open("rb") as handle:
        while True:
            try:
                ordinal, location, content = pickle.load(handle)
            except EOFError:
                return
            yield DocumentChunk(int(ordinal), str(location), str(content))


def extract_to_file(source: Path, output: Path) -> int:
    # The extraction broker uses this marker to execute compatibility adapters
    # directly inside this killable child instead of recursively spawning more
    # workers. Keep it scoped so direct unit calls cannot leak worker state.
    previous = os.environ.get("DOCSEEK_LEGACY_WORKER")
    os.environ["DOCSEEK_LEGACY_WORKER"] = "1"
    try:
        return write_chunk_file(
            output,
            DEFAULT_EXTRACTION_BROKER.iter_chunks(Path(source)),
        )
    finally:
        if previous is None:
            os.environ.pop("DOCSEEK_LEGACY_WORKER", None)
        else:
            os.environ["DOCSEEK_LEGACY_WORKER"] = previous


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        return 2

    source = Path(args[0])
    output = Path(args[1])
    error_path = output.with_name(output.name + ".error.txt")
    error_path.unlink(missing_ok=True)
    try:
        extract_to_file(source, output)
    except BaseException as exc:  # worker boundary: persist failures for windowed builds
        try:
            error_path.write_text(
                f"{type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
