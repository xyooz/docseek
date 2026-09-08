from __future__ import annotations

import os
import pickle
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Iterable

from .chunks import DocumentChunk
from .document_adapters import DEFAULT_ADAPTER_REGISTRY, AdapterUnavailable
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


def _adapter_is_usable(adapter, source: Path) -> bool:
    supports_path = getattr(adapter, "supports_path", None)
    if supports_path is not None:
        return bool(supports_path(source))
    return bool(adapter.is_available())


def _adapter_for_name(source: Path, adapter_name: str):
    for adapter in DEFAULT_ADAPTER_REGISTRY.candidates_for(source):
        if adapter.name != adapter_name:
            continue
        if not _adapter_is_usable(adapter, source):
            raise AdapterUnavailable(
                f"解析器 {adapter_name} 当前不可用于 {source.suffix.lower()}"
            )
        return adapter
    raise AdapterUnavailable(
        f"解析器 {adapter_name} 未注册用于 {source.suffix.lower()}"
    )


def extract_to_file(
    source: Path,
    output: Path,
    *,
    adapter_name: str | None = None,
    on_progress: Callable[[str, int], None] | None = None,
) -> int:
    """Extract one document inside the killable worker process.

    When ``adapter_name`` is supplied the parent is explicitly trying one
    compatibility backend in a fallback chain. Keeping adapter selection inside
    this process means a crash in Calamine/Tika/WPS cannot escape into the
    desktop process.
    """
    source = Path(source)
    previous = os.environ.get("DOCSEEK_LEGACY_WORKER")
    os.environ["DOCSEEK_LEGACY_WORKER"] = "1"
    try:
        if adapter_name:
            adapter = _adapter_for_name(source, adapter_name)
            chunks = adapter.iter_chunks(source, on_progress=on_progress)
        else:
            chunks = DEFAULT_EXTRACTION_BROKER.iter_chunks(
                source,
                on_progress=on_progress,
            )
        return write_chunk_file(output, chunks)
    finally:
        if previous is None:
            os.environ.pop("DOCSEEK_LEGACY_WORKER", None)
        else:
            os.environ["DOCSEEK_LEGACY_WORKER"] = previous


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    adapter_name: str | None = None
    progress_path: Path | None = None
    if len(args) == 4 and args[0] == "--adapter":
        adapter_name = args[1]
        source = Path(args[2])
        output = Path(args[3])
    elif len(args) == 6 and args[0] == "--adapter" and args[4] == "--progress":
        adapter_name = args[1]
        source = Path(args[2])
        output = Path(args[3])
        progress_path = Path(args[5])
    elif len(args) == 4 and args[2] == "--progress":
        source = Path(args[0])
        output = Path(args[1])
        progress_path = Path(args[3])
    elif len(args) == 2:
        source = Path(args[0])
        output = Path(args[1])
    else:
        return 2

    error_path = output.with_name(output.name + ".error.txt")
    error_path.unlink(missing_ok=True)
    progress_handle = None
    progress_callback = None
    if progress_path is not None:
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_handle = progress_path.open("a", encoding="ascii")

        def progress_callback(location: str, current: int) -> None:
            import json

            progress_handle.write(
                json.dumps([str(location), int(current)], ensure_ascii=True) + "\n"
            )
            progress_handle.flush()
    try:
        extract_to_file(
            source,
            output,
            adapter_name=adapter_name,
            on_progress=progress_callback,
        )
    except BaseException as exc:  # worker boundary: persist failures for windowed builds
        try:
            error_path.write_text(
                f"{type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return 1
    finally:
        if progress_handle is not None:
            progress_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
