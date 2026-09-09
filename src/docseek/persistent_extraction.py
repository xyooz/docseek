"""Single-process persistent extraction worker controller.

This module is the M8-A worker core.  It is deliberately not wired into the
indexer yet: ``legacy_isolation.iter_legacy_chunks_isolated`` remains the
production path until the lifecycle and fallback contract has been validated.

The controller talks to one ``legacy_worker`` child over JSON lines while the
child keeps the existing pickle chunk spool for results.  A parser exception is
reported as one failed request and does not terminate the child.  A crash,
timeout, stall, or cancellation makes the child unusable; the next request
starts a fresh process.
"""

from __future__ import annotations

import json
import itertools
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from .chunks import DocumentChunk
from .legacy_isolation import (
    LEGACY_ADAPTER_STALL_TIMEOUT_SECONDS,
    LEGACY_ADAPTER_TIMEOUT_SECONDS,
    LEGACY_PARSE_TIMEOUT_SECONDS,
    LegacyExtractionCancelled,
    LegacyExtractionError,
    LegacyExtractionTimeout,
)
from .legacy_worker import iter_chunk_file


PERSISTENT_WORKER_PROTOCOL_VERSION = 1
WORKER_READY_TIMEOUT_SECONDS = 10.0
WORKER_POLL_SECONDS = 0.05
WORKER_TERMINATE_WAIT_SECONDS = 0.20


class PersistentWorkerError(LegacyExtractionError):
    """The persistent worker protocol or lifecycle failed."""


class PersistentWorkerCrashed(PersistentWorkerError):
    """The child exited without returning the active request's result."""


class PersistentWorkerBusy(PersistentWorkerError):
    """A second extraction was submitted while one request was active."""


def persistent_worker_command() -> list[str]:
    """Build a source or PyInstaller-frozen persistent worker command."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--docseek-extract-worker", "--persistent"]
    return [sys.executable, "-m", "docseek.legacy_worker", "--persistent"]


def _terminate_process(
    process: subprocess.Popen[str],
    *,
    wait_seconds: float = WORKER_TERMINATE_WAIT_SECONDS,
) -> None:
    """Terminate and, if needed, kill a child without leaving it unreaped."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=max(0.01, wait_seconds))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


class PersistentExtractionWorker:
    """Own one reusable, killable extraction worker process.

    ``extract`` performs the request synchronously and returns an iterator over
    the worker's pickle spool.  The spool is removed when that iterator is
    exhausted or closed.  Only one request may be active at a time; ``cancel``
    and ``terminate`` intentionally do not wait on that request lock so a UI
    thread can stop a parser while the indexing thread is blocked.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = LEGACY_PARSE_TIMEOUT_SECONDS,
        adapter_timeout_seconds: float = LEGACY_ADAPTER_TIMEOUT_SECONDS,
        stall_timeout_seconds: float | None = LEGACY_ADAPTER_STALL_TIMEOUT_SECONDS,
        command_factory: Callable[[], Sequence[str]] | None = None,
        output_dir: Path | None = None,
    ) -> None:
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.adapter_timeout_seconds = max(0.01, float(adapter_timeout_seconds))
        self.stall_timeout_seconds = (
            None
            if stall_timeout_seconds is None
            else max(0.01, float(stall_timeout_seconds))
        )
        self._command_factory = command_factory or persistent_worker_command
        self._lifecycle_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._messages: Queue[dict[str, Any] | None] = Queue()
        self._stderr_tail: deque[str] = deque(maxlen=32)
        self._reader_threads: list[threading.Thread] = []
        self._state = "new"
        self._active_request_id: int | None = None
        self._termination_reason: BaseException | None = None
        self._request_ids = itertools.count(1)
        self._output_paths: set[Path] = set()
        self._closed = False

        if output_dir is None:
            self._temporary_output_dir = tempfile.TemporaryDirectory(
                prefix="docseek-persistent-extract-"
            )
            self._output_dir = Path(self._temporary_output_dir.name)
        else:
            self._temporary_output_dir = None
            self._output_dir = Path(output_dir)
            self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def state(self) -> str:
        with self._lifecycle_lock:
            return self._state

    @property
    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            return self._process is not None and self._process.poll() is None

    @property
    def active_request_id(self) -> int | None:
        with self._lifecycle_lock:
            return self._active_request_id

    def __enter__(self) -> "PersistentExtractionWorker":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.close()
        return False

    def start(self) -> None:
        """Start the worker if it is not already alive."""
        with self._lifecycle_lock:
            self._ensure_open_locked()
            if self._process is not None and self._process.poll() is None:
                return
            if self._active_request_id is not None:
                raise PersistentWorkerBusy("persistent extraction request is active")
            self._spawn_locked()

    def restart(self) -> None:
        """Replace a dead or idle worker with a fresh process."""
        if not self._request_lock.acquire(blocking=False):
            raise PersistentWorkerBusy("cannot restart during an extraction request")
        try:
            with self._lifecycle_lock:
                self._ensure_open_locked()
                self._stop_locked()
                self._spawn_locked()
        finally:
            self._request_lock.release()

    def extract(
        self,
        source: Path,
        *,
        adapter_name: str | None = None,
        on_progress: Callable[[str, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        timeout_seconds: float | None = None,
        stall_timeout_seconds: float | None = None,
    ) -> Iterator[DocumentChunk]:
        """Extract one document and return an iterator over its spool.

        The request itself is completed before this method returns.  This is
        intentional: the worker can accept the next request only after the
        caller has received a complete result and can replay the current spool.
        """
        if not self._request_lock.acquire(blocking=False):
            raise PersistentWorkerBusy("only one persistent extraction request is allowed")

        source = Path(source)
        output: Path | None = None
        process: subprocess.Popen[str] | None = None
        try:
            self.start()
            with self._lifecycle_lock:
                process = self._process
                if process is None or process.poll() is not None:
                    raise PersistentWorkerCrashed("persistent extraction worker is not alive")
                request_id = next(self._request_ids)
                output = self._output_dir / f"chunks-{request_id}.bin"
                output.unlink(missing_ok=True)
                self._output_paths.add(output)
                self._active_request_id = request_id
                self._termination_reason = None

            payload: dict[str, Any] = {
                "type": "extract",
                "request_id": request_id,
                "source": str(source),
                "output": str(output),
            }
            if adapter_name is not None:
                payload["adapter_name"] = str(adapter_name)
            self._send(payload)

            chunk_count, returned_output = self._wait_for_result(
                request_id,
                expected_output=output,
                on_progress=on_progress,
                cancelled=cancelled,
                timeout_seconds=(
                    self.timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
                stall_timeout_seconds=(
                    self.stall_timeout_seconds
                    if stall_timeout_seconds is None
                    else stall_timeout_seconds
                ),
            )
            del chunk_count
            return self._replay_and_cleanup(returned_output)
        except BaseException:
            if output is not None:
                self._remove_output(output)
            raise
        finally:
            with self._lifecycle_lock:
                if self._active_request_id is not None:
                    self._active_request_id = None
                if self._closed:
                    self._state = "closed"
                elif self._process is not None and self._process.poll() is None:
                    self._state = "ready"
                else:
                    self._state = "dead"
                self._termination_reason = None
            self._request_lock.release()

    def cancel(self) -> bool:
        """Kill the active worker so an uncooperative parser cannot linger."""
        with self._lifecycle_lock:
            if self._active_request_id is None:
                return False
            self._termination_reason = LegacyExtractionCancelled(
                "兼容格式解析已取消"
            )
            self._stop_locked()
            return True

    def terminate(self) -> None:
        """Force the current worker down and mark it dead."""
        with self._lifecycle_lock:
            if self._active_request_id is not None:
                self._termination_reason = PersistentWorkerError(
                    "persistent extraction worker was terminated"
                )
            self._stop_locked()

    def close(self) -> None:
        """Gracefully shut down an idle worker, or kill an active one."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            active = self._active_request_id is not None

            if process is not None and process.poll() is None:
                if active:
                    self._termination_reason = LegacyExtractionCancelled(
                        "兼容格式解析已因 worker 关闭而取消"
                    )
                    _terminate_process(process)
                else:
                    try:
                        self._send_locked({"type": "shutdown"})
                        self._wait_for_shutdown_locked(process)
                    except (OSError, PersistentWorkerError, TimeoutError):
                        _terminate_process(process)
                self._close_process_pipes(process)

            self._state = "closed"
            self._cleanup_outputs_locked()
            if self._temporary_output_dir is not None:
                self._temporary_output_dir.cleanup()
                self._temporary_output_dir = None

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise PersistentWorkerError("persistent extraction worker is closed")

    def _spawn_locked(self) -> None:
        self._ensure_open_locked()
        command = [str(part) for part in self._command_factory()]
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            self._state = "dead"
            raise PersistentWorkerError(
                f"无法启动 persistent extraction worker：{exc}"
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        messages: Queue[dict[str, Any] | None] = Queue()
        stderr_tail: deque[str] = deque(maxlen=32)
        self._messages = messages
        self._process = process
        self._stderr_tail = stderr_tail
        self._state = "starting"

        def read_stdout() -> None:
            try:
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except json.JSONDecodeError:
                        messages.put(
                            {
                                "type": "protocol_error",
                                "detail": line.strip(),
                            }
                        )
            except (OSError, ValueError):
                pass
            finally:
                messages.put(None)

        def read_stderr() -> None:
            try:
                for line in process.stderr:
                    stderr_tail.append(line.rstrip())
            except (OSError, ValueError):
                pass

        stdout_thread = threading.Thread(
            target=read_stdout,
            name="docseek-persistent-worker-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_stderr,
            name="docseek-persistent-worker-stderr",
            daemon=True,
        )
        # A respawn replaces the previous process and its reader threads.  The
        # old daemon threads have closed pipes and can finish independently;
        # do not retain their references for the lifetime of this controller.
        self._reader_threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()

        try:
            message = self._next_message_from(messages, WORKER_READY_TIMEOUT_SECONDS)
            if message is None:
                raise self._crashed_error(process)
            if message.get("type") != "ready":
                raise PersistentWorkerError(
                    f"persistent extraction worker 未发送 ready：{message!r}"
                )
            if int(message.get("protocol_version", -1)) != PERSISTENT_WORKER_PROTOCOL_VERSION:
                raise PersistentWorkerError(
                    "persistent extraction worker protocol version mismatch"
                )
            self._state = "ready"
        except BaseException:
            _terminate_process(process)
            self._close_process_pipes(process)
            self._state = "dead"
            raise

    def _send(self, payload: dict[str, Any]) -> None:
        with self._lifecycle_lock:
            self._send_locked(payload)

    def _send_locked(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise self._crashed_error(process)
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            reason = self._crashed_error(process)
            self._termination_reason = reason
            self._stop_locked()
            raise reason from exc

    @staticmethod
    def _next_message_from(
        messages: Queue[dict[str, Any] | None], timeout_seconds: float
    ) -> dict[str, Any] | None:
        try:
            return messages.get(timeout=max(0.01, timeout_seconds))
        except Empty:
            raise PersistentWorkerError("persistent extraction worker response timeout")

    def _wait_for_result(
        self,
        request_id: int,
        *,
        expected_output: Path,
        on_progress: Callable[[str, int], None] | None,
        cancelled: Callable[[], bool] | None,
        timeout_seconds: float,
        stall_timeout_seconds: float | None,
    ) -> tuple[int, Path]:
        timeout = max(0.01, min(float(timeout_seconds), self.adapter_timeout_seconds))
        deadline = time.monotonic() + timeout
        last_progress_at = time.monotonic()
        stall_limit = (
            None
            if stall_timeout_seconds is None or on_progress is None
            else max(0.01, min(float(stall_timeout_seconds), timeout))
        )

        while True:
            if cancelled is not None and cancelled():
                self.cancel()
                raise LegacyExtractionCancelled("兼容格式解析已取消")

            now = time.monotonic()
            if now >= deadline:
                error = LegacyExtractionTimeout(
                    f"兼容格式解析超过 {timeout:g} 秒，已终止 persistent worker"
                )
                with self._lifecycle_lock:
                    self._termination_reason = error
                    self._stop_locked()
                raise error
            if stall_limit is not None and now - last_progress_at >= stall_limit:
                error = LegacyExtractionTimeout(
                    f"兼容格式解析连续 {stall_limit:g} 秒没有进展，已终止 persistent worker"
                )
                with self._lifecycle_lock:
                    self._termination_reason = error
                    self._stop_locked()
                raise error

            wait_seconds = min(WORKER_POLL_SECONDS, deadline - now)
            if stall_limit is not None:
                wait_seconds = min(wait_seconds, stall_limit - (now - last_progress_at))
            try:
                message = self._messages.get(timeout=max(0.01, wait_seconds))
            except Empty:
                continue

            if message is None:
                with self._lifecycle_lock:
                    reason = self._termination_reason
                if reason is not None:
                    raise reason
                raise self._crashed_error(self._process)

            message_type = message.get("type")
            if message_type == "progress":
                if message.get("request_id") != request_id:
                    raise PersistentWorkerError(
                        f"progress request_id mismatch: {message!r}"
                    )
                last_progress_at = time.monotonic()
                if on_progress is not None:
                    on_progress(str(message.get("location", "")), int(message.get("current", 0)))
                continue

            if message.get("request_id") != request_id:
                raise PersistentWorkerError(
                    f"worker response request_id mismatch: {message!r}"
                )

            if message_type == "result":
                returned_output = Path(str(message.get("output", "")))
                if returned_output != expected_output:
                    raise PersistentWorkerError(
                        f"worker returned unexpected output path: {returned_output}"
                    )
                return int(message.get("chunk_count", 0)), returned_output

            if message_type == "error":
                detail = str(message.get("error", "persistent extraction failed"))
                error_type = str(message.get("error_type", "Exception"))
                raise PersistentWorkerError(f"{error_type}: {detail}")

            raise PersistentWorkerError(f"unknown worker response: {message!r}")

    def _wait_for_shutdown_locked(self, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            remaining = deadline - time.monotonic()
            try:
                message = self._messages.get(timeout=max(0.01, min(0.05, remaining)))
            except Empty:
                continue
            if message is None:
                return
            if message.get("type") == "shutdown_ack":
                process.wait(timeout=max(0.01, remaining))
                return
        raise PersistentWorkerError("persistent extraction worker shutdown timeout")

    def _crashed_error(self, process: subprocess.Popen[str] | None) -> PersistentWorkerCrashed:
        code = process.poll() if process is not None else None
        stderr = " ".join(item for item in self._stderr_tail if item)
        detail = f"persistent extraction worker exited (code={code})"
        if stderr:
            detail += f": {stderr[-1000:]}"
        if process is not None and code is not None:
            self._close_process_pipes(process)
        self._state = "dead"
        return PersistentWorkerCrashed(detail)

    def _stop_locked(self) -> None:
        process = self._process
        if process is not None:
            _terminate_process(process)
            self._close_process_pipes(process)
        if self._state != "closed":
            self._state = "dead"

    @staticmethod
    def _close_process_pipes(process: subprocess.Popen[str]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _replay_and_cleanup(self, output: Path) -> Iterator[DocumentChunk]:
        try:
            yield from iter_chunk_file(output)
        finally:
            self._remove_output(output)

    def _remove_output(self, output: Path) -> None:
        try:
            output.unlink(missing_ok=True)
        except OSError:
            pass
        with self._lifecycle_lock:
            self._output_paths.discard(output)

    def _cleanup_outputs_locked(self) -> None:
        for output in tuple(self._output_paths):
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
        self._output_paths.clear()
