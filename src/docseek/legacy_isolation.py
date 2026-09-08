from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .chunks import DocumentChunk
from .document_adapters import DEFAULT_ADAPTER_REGISTRY
from .legacy_worker import iter_chunk_file


LEGACY_PARSE_TIMEOUT_SECONDS = 120.0
LEGACY_ADAPTER_TIMEOUT_SECONDS = 45.0
LEGACY_WORKER_POLL_SECONDS = 0.10


class LegacyExtractionError(RuntimeError):
    pass


class LegacyExtractionTimeout(TimeoutError):
    pass


class LegacyExtractionCancelled(RuntimeError):
    pass


def _adapter_is_usable(adapter, source: Path) -> bool:
    supports_path = getattr(adapter, "supports_path", None)
    if supports_path is not None:
        return bool(supports_path(source))
    return bool(adapter.is_available())


def available_legacy_adapter_names(source: Path) -> tuple[str, ...]:
    """Return usable compatibility parsers in production priority order."""
    source = Path(source)
    names: list[str] = []
    for adapter in DEFAULT_ADAPTER_REGISTRY.candidates_for(source):
        try:
            if _adapter_is_usable(adapter, source):
                names.append(adapter.name)
        except Exception:
            continue
    return tuple(names)


def legacy_worker_command(
    source: Path,
    output: Path,
    *,
    adapter_name: str | None = None,
    progress: Path | None = None,
) -> list[str]:
    """Build the worker command for source and PyInstaller-frozen executions."""
    worker_args: list[str] = []
    if adapter_name:
        worker_args.extend(["--adapter", adapter_name])
    worker_args.extend([str(source), str(output)])
    if progress is not None:
        worker_args.extend(["--progress", str(progress)])

    if getattr(sys, "frozen", False):
        return [sys.executable, "--docseek-extract-worker", *worker_args]
    return [sys.executable, "-m", "docseek.legacy_worker", *worker_args]


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _close_worker_stderr(process: subprocess.Popen[bytes]) -> None:
    if process.stderr is not None and not process.stderr.closed:
        process.stderr.close()


def wait_for_worker(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    cancelled: Callable[[], bool] | None = None,
    progress_path: Path | None = None,
    on_progress: Callable[[str, int], None] | None = None,
) -> int:
    deadline = time.monotonic() + max(0.01, float(timeout_seconds))
    progress_offset = 0

    def drain_progress() -> None:
        nonlocal progress_offset
        if progress_path is None or on_progress is None or not progress_path.exists():
            return
        try:
            data = progress_path.read_bytes()
        except OSError:
            return
        pending = data[progress_offset:]
        progress_offset = len(data)
        for line in pending.splitlines():
            try:
                location, current = json.loads(line.decode("ascii"))
                on_progress(str(location), int(current))
            except (ValueError, TypeError, UnicodeDecodeError):
                continue

    while True:
        drain_progress()
        code = process.poll()
        if code is not None:
            drain_progress()
            return int(code)
        if cancelled is not None and cancelled():
            _terminate_process(process)
            raise LegacyExtractionCancelled("兼容格式解析已取消")
        if time.monotonic() >= deadline:
            _terminate_process(process)
            raise LegacyExtractionTimeout(
                f"兼容格式解析超过 {timeout_seconds:g} 秒，已终止该解析进程"
            )
        time.sleep(LEGACY_WORKER_POLL_SECONDS)


def _worker_error_detail(
    *,
    error_path: Path,
    stderr: bytes,
    exit_code: int,
) -> str:
    detail = ""
    if error_path.exists():
        try:
            detail = error_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            detail = ""
    if not detail and stderr:
        detail = stderr.decode("utf-8", errors="replace").strip()
    if not detail:
        detail = f"解析子进程退出码 {exit_code}"
    return detail


def iter_legacy_chunks_isolated(
    source: Path,
    *,
    timeout_seconds: float = LEGACY_PARSE_TIMEOUT_SECONDS,
    adapter_timeout_seconds: float = LEGACY_ADAPTER_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[str, int], None] | None = None,
) -> Iterator[DocumentChunk]:
    """Extract one compatibility document through a bounded parser cascade."""
    source = Path(source)
    adapter_names = available_legacy_adapter_names(source)
    if not adapter_names:
        raise LegacyExtractionError(
            f"{source.suffix.lower()} 当前没有可用的本地兼容解析器"
        )

    started = time.monotonic()
    attempts: list[str] = []

    with tempfile.TemporaryDirectory(prefix="docseek-extract-") as temp_dir:
        temp_root = Path(temp_dir)

        for attempt_no, adapter_name in enumerate(adapter_names, start=1):
            if cancelled is not None and cancelled():
                raise LegacyExtractionCancelled("兼容格式解析已取消")

            elapsed = time.monotonic() - started
            remaining = float(timeout_seconds) - elapsed
            if remaining <= 0:
                summary = "；".join(attempts) if attempts else "尚未完成任何解析器"
                raise LegacyExtractionTimeout(
                    f"兼容格式解析总时限 {timeout_seconds:g} 秒已用尽：{summary}"
                )

            attempt_timeout = min(
                max(0.01, float(adapter_timeout_seconds)),
                max(0.01, remaining),
            )
            output = temp_root / f"chunks-{attempt_no}.bin"
            error_path = output.with_name(output.name + ".error.txt")
            progress = temp_root / f"progress-{attempt_no}.jsonl"
            progress.unlink(missing_ok=True)
            command_kwargs: dict[str, object] = {
                "adapter_name": adapter_name,
            }
            # Preserve the historical call signature when no progress channel
            # is requested.  This keeps embedders that replace the command
            # factory backwards-compatible with the isolation API.
            if on_progress is not None:
                command_kwargs["progress"] = progress
            command = legacy_worker_command(source, output, **command_kwargs)

            kwargs: dict[str, object] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
            }
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
            try:
                try:
                    code = wait_for_worker(
                        process,
                        timeout_seconds=attempt_timeout,
                        cancelled=cancelled,
                        progress_path=progress,
                        on_progress=on_progress,
                    )
                except LegacyExtractionTimeout:
                    attempts.append(f"{adapter_name}: 超时")
                    _close_worker_stderr(process)
                    continue
                except BaseException:
                    _close_worker_stderr(process)
                    raise
            finally:
                if process.poll() is None:
                    _terminate_process(process)

            stderr = b""
            if process.stderr is not None:
                try:
                    stderr = process.stderr.read()[-4000:]
                finally:
                    _close_worker_stderr(process)

            if code != 0:
                detail = _worker_error_detail(
                    error_path=error_path,
                    stderr=stderr,
                    exit_code=code,
                )
                attempts.append(f"{adapter_name}: {detail}")
                continue

            if not output.exists():
                attempts.append(f"{adapter_name}: 未生成内容结果")
                continue

            yield from iter_chunk_file(output)
            return

    summary = "；".join(attempts) if attempts else "没有解析器成功返回内容"
    raise LegacyExtractionError(f"所有兼容解析器均失败：{summary}")
