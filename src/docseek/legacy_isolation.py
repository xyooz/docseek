from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .chunks import DocumentChunk
from .legacy_worker import iter_chunk_file


LEGACY_PARSE_TIMEOUT_SECONDS = 120.0
LEGACY_WORKER_POLL_SECONDS = 0.10


class LegacyExtractionError(RuntimeError):
    pass


class LegacyExtractionTimeout(TimeoutError):
    pass


class LegacyExtractionCancelled(RuntimeError):
    pass


def legacy_worker_command(source: Path, output: Path) -> list[str]:
    """Build the worker command for source and PyInstaller-frozen executions."""
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--docseek-extract-worker",
            str(source),
            str(output),
        ]
    return [
        sys.executable,
        "-m",
        "docseek.legacy_worker",
        str(source),
        str(output),
    ]


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def wait_for_worker(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    deadline = time.monotonic() + max(0.01, float(timeout_seconds))
    while True:
        code = process.poll()
        if code is not None:
            return int(code)
        if cancelled is not None and cancelled():
            _terminate_process(process)
            raise LegacyExtractionCancelled("旧格式解析已取消")
        if time.monotonic() >= deadline:
            _terminate_process(process)
            raise LegacyExtractionTimeout(
                f"旧格式解析超过 {timeout_seconds:g} 秒，已终止该解析进程"
            )
        time.sleep(LEGACY_WORKER_POLL_SECONDS)


def iter_legacy_chunks_isolated(
    source: Path,
    *,
    timeout_seconds: float = LEGACY_PARSE_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[DocumentChunk]:
    """Extract one legacy document outside the desktop/indexer process.

    A parser crash, hang or native-library deadlock is confined to this child.
    The parent receives only a trusted temporary chunk stream after a clean exit.
    """
    source = Path(source)
    with tempfile.TemporaryDirectory(prefix="docseek-extract-") as temp_dir:
        output = Path(temp_dir) / "chunks.bin"
        error_path = output.with_name(output.name + ".error.txt")
        command = legacy_worker_command(source, output)

        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
        code = wait_for_worker(
            process,
            timeout_seconds=timeout_seconds,
            cancelled=cancelled,
        )
        stderr = b""
        if process.stderr is not None:
            stderr = process.stderr.read()[-4000:]

        if code != 0:
            detail = ""
            if error_path.exists():
                try:
                    detail = error_path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    detail = ""
            if not detail and stderr:
                detail = stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = f"旧格式解析子进程退出码 {code}"
            raise LegacyExtractionError(detail)

        if not output.exists():
            raise LegacyExtractionError("旧格式解析子进程未生成可读取的内容结果")

        yield from iter_chunk_file(output)
