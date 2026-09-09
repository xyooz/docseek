"""Measure parser cost versus per-file process isolation cost.

This is intentionally a benchmark-only experiment.  It does not change the
production isolation implementation or its pickle spool protocol.

The benchmark worker uses the same registered adapter and the same
``DOCSEEK_LEGACY_WORKER=1`` direct-adapter boundary as ``legacy_worker.py``;
its JSON-lines protocol only makes startup, parser, serialization, and
readback timings observable and allows a single worker to be reused.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterable

# Keep the benchmark runnable from a clean checkout as well as from an
# editable installation.  The child worker executes this same file.
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from docseek.document_adapters import DEFAULT_ADAPTER_REGISTRY
from docseek.legacy_isolation import available_legacy_adapter_names
from docseek.legacy_worker import _adapter_for_name


SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_TIMEOUT_SECONDS = 60.0
HANG_TIMEOUT_SECONDS = 2.0
WORKLOADS = {
    "docx": (".docx", "testWORD.docx", 100),
    "xlsx": (".xlsx", "testEXCEL.xlsx", 50),
    "pptx": (".pptx", "testPPT.pptx", 100),
    "pdf": (".pdf", "testPDF.pdf", 100),
}


class WorkerError(RuntimeError):
    """The benchmark worker exited or returned an invalid response."""


class WorkerTimeout(TimeoutError):
    """The benchmark worker did not answer within the requested deadline."""


@dataclass(slots=True, frozen=True)
class RequestMeasurement:
    request_seconds: float
    write_seconds: float
    readback_seconds: float
    parser_seconds: float
    serialization_seconds: float
    chunks: list[list[Any]]


def _official_fixture_path(fixture_name: str) -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "official"
        / fixture_name
    )


def _chunk_rows(chunks: Iterable[Any]) -> list[list[Any]]:
    return [
        [int(chunk.ordinal), str(chunk.location), str(chunk.content)]
        for chunk in chunks
    ]


def _extract_with_adapter(path: Path, adapter_name: str) -> list[list[Any]]:
    """Extract through the same direct adapter boundary as the legacy worker."""
    previous = os.environ.get("DOCSEEK_LEGACY_WORKER")
    os.environ["DOCSEEK_LEGACY_WORKER"] = "1"
    try:
        adapter = _adapter_for_name(Path(path), adapter_name)
        return _chunk_rows(adapter.iter_chunks(Path(path)))
    finally:
        if previous is None:
            os.environ.pop("DOCSEEK_LEGACY_WORKER", None)
        else:
            os.environ["DOCSEEK_LEGACY_WORKER"] = previous


def _worker_response(request: dict[str, Any]) -> dict[str, Any]:
    operation = str(request.get("op", "extract"))
    if operation == "shutdown":
        return {"id": request.get("id"), "ok": True, "shutdown": True}
    if operation == "hang":
        while True:
            time.sleep(1.0)
    if operation == "crash":
        os._exit(23)
    if operation != "extract":
        raise ValueError(f"unknown benchmark worker operation: {operation}")

    path = Path(str(request["path"]))
    adapter_name = str(request["adapter"])
    parser_started = time.perf_counter()
    try:
        chunks = _extract_with_adapter(path, adapter_name)
    except BaseException as exc:
        return {
            "id": request.get("id"),
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "parser_seconds": time.perf_counter() - parser_started,
        }
    parser_seconds = time.perf_counter() - parser_started

    payload = {
        "id": request.get("id"),
        "ok": True,
        "chunks": chunks,
        "parser_seconds": parser_seconds,
    }
    serialization_started = time.perf_counter()
    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload["serialization_seconds"] = time.perf_counter() - serialization_started
    return payload


def _write_worker_payload(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def _worker_main(mode: str) -> int:
    """Run the benchmark-only JSON-lines worker."""
    sys.stdout.write(
        json.dumps({"kind": "ready", "pid": os.getpid()}) + "\n"
    )
    sys.stdout.flush()
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        try:
            payload = _worker_response(request)
        except BaseException as exc:
            payload = {
                "id": request.get("id"),
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        _write_worker_payload(payload)
        if payload.get("shutdown"):
            return 0
        if mode == "oneshot":
            return 0
    return 0


class BenchmarkWorker:
    """Small cross-platform parent-side controller for the PoC worker."""

    def __init__(self, mode: str, timeout_seconds: float) -> None:
        self.mode = mode
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.process: subprocess.Popen[str] | None = None
        self._lines: Queue[str | None] = Queue()
        self._reader: threading.Thread | None = None
        self.startup_seconds = 0.0

    def start(self) -> float:
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), "--worker", self.mode],
            **kwargs,
        )
        assert self.process.stdout is not None

        def read_lines() -> None:
            try:
                for line in self.process.stdout:
                    self._lines.put(line)
            finally:
                self._lines.put(None)

        self._reader = threading.Thread(
            target=read_lines,
            name="docseek-isolation-benchmark-reader",
            daemon=True,
        )
        self._reader.start()
        ready = self._next_line(self.timeout_seconds)
        try:
            payload = json.loads(ready)
        except json.JSONDecodeError as exc:
            raise WorkerError(f"invalid worker ready message: {ready!r}") from exc
        if payload.get("kind") != "ready":
            raise WorkerError(f"worker did not become ready: {payload!r}")
        self.startup_seconds = time.perf_counter() - started
        return self.startup_seconds

    def _next_line(self, timeout_seconds: float) -> str:
        try:
            line = self._lines.get(timeout=max(0.01, timeout_seconds))
        except Empty as exc:
            raise WorkerTimeout("benchmark worker response timed out") from exc
        if line is None:
            code = self.process.poll() if self.process is not None else None
            raise WorkerError(f"benchmark worker exited before responding: {code}")
        return line

    def request(
        self,
        request: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> RequestMeasurement:
        if self.process is None or self.process.stdin is None:
            raise WorkerError("benchmark worker is not running")
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        write_seconds = self.write_request(request)
        read_started = time.perf_counter()
        line = self._next_line(timeout)
        readback_seconds = time.perf_counter() - read_started
        request_seconds = write_seconds + readback_seconds
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkerError(f"invalid worker response: {line!r}") from exc
        if payload.get("id") != request.get("id"):
            raise WorkerError(f"worker response id mismatch: {payload!r}")
        if not payload.get("ok"):
            raise WorkerError(
                f"{payload.get('error_type', 'WorkerError')}: "
                f"{payload.get('error', 'unknown extraction failure')}"
            )
        return RequestMeasurement(
            request_seconds=request_seconds,
            write_seconds=write_seconds,
            readback_seconds=readback_seconds,
            parser_seconds=float(payload.get("parser_seconds", 0.0)),
            serialization_seconds=float(payload.get("serialization_seconds", 0.0)),
            chunks=payload.get("chunks", []),
        )

    def write_request(self, request: dict[str, Any]) -> float:
        """Write a request without waiting, for timeout/cancel experiments."""
        if self.process is None or self.process.stdin is None:
            raise WorkerError("benchmark worker is not running")
        encoded_request = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        started = time.perf_counter()
        self.process.stdin.write(encoded_request + "\n")
        self.process.stdin.flush()
        return time.perf_counter() - started

    def send_control(
        self,
        operation: str,
        request_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        if self.process is None or self.process.stdin is None:
            raise WorkerError("benchmark worker is not running")
        self.write_request({"id": request_id, "op": operation})
        line = self._next_line(
            self.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        payload = json.loads(line)
        if payload.get("id") != request_id or not payload.get("ok"):
            raise WorkerError(f"invalid control response: {payload!r}")

    def shutdown(self) -> float:
        if self.process is None:
            return 0.0
        if self.process.poll() is None:
            started = time.perf_counter()
            try:
                self.send_control("shutdown", -1, timeout_seconds=self.timeout_seconds)
            except (WorkerError, WorkerTimeout):
                self.terminate()
            try:
                self.process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                self.terminate()
            elapsed = time.perf_counter() - started
        else:
            elapsed = 0.0
        return elapsed

    def wait_for_exit(self) -> float:
        """Measure natural one-shot worker exit after its result is written."""
        if self.process is None:
            return 0.0
        started = time.perf_counter()
        try:
            self.process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            self.terminate()
        return time.perf_counter() - started

    def terminate(self) -> float:
        if self.process is None or self.process.poll() is not None:
            return 0.0
        started = time.perf_counter()
        self.process.terminate()
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5.0)
        return time.perf_counter() - started


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summary(values: list[float]) -> dict[str, float | None]:
    p95 = _percentile(values, 0.95)
    return {
        "p50_ms": (statistics.median(values) * 1000) if values else None,
        "p95_ms": (p95 * 1000) if p95 is not None else None,
        "average_ms": (statistics.mean(values) * 1000) if values else None,
    }


def _create_workload(root: Path, workload: str, count: int) -> list[Path]:
    extension, fixture_name, _default_count = WORKLOADS[workload]
    source = _official_fixture_path(fixture_name)
    if not source.is_file():
        raise FileNotFoundError(f"official fixture not found: {source}")
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(count):
        destination = root / f"document_{index:06d}{extension}"
        shutil.copy2(source, destination)
        paths.append(destination)
    return paths


def _select_adapter(path: Path) -> str:
    names = available_legacy_adapter_names(path)
    if not names:
        # Make the error identify the registry state instead of failing later
        # inside a child process with a less useful broken-pipe message.
        candidates = [adapter.name for adapter in DEFAULT_ADAPTER_REGISTRY.candidates_for(path)]
        raise RuntimeError(
            f"no usable adapter for {path.suffix}: candidates={candidates!r}"
        )
    # M7-C compares the parser/isolation boundary for the same direct Office
    # and PDF adapter that the Windows benchmark environment uses.  A local
    # developer environment may have the optional python-calamine adapter
    # installed; that native adapter is a separate experiment and can panic on
    # a fixture before the production cascade falls back to ``direct``.
    if "direct" in names:
        return "direct"
    return names[0]


def run_direct(paths: list[Path], adapter_name: str) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    results: list[list[list[Any]]] = []
    started = time.perf_counter()
    for path in paths:
        file_started = time.perf_counter()
        parser_started = time.perf_counter()
        chunks = _extract_with_adapter(path, adapter_name)
        parser_seconds = time.perf_counter() - parser_started
        results.append(chunks)
        records.append(
            {
                "path": path.name,
                "request_seconds": time.perf_counter() - file_started,
                "parser_seconds": parser_seconds,
                "serialization_seconds": 0.0,
                "readback_seconds": 0.0,
                "chunks": len(chunks),
            }
        )
    total = time.perf_counter() - started
    return {
        "mode": "direct",
        "files": len(paths),
        "total_seconds": total,
        "parser_total": sum(record["parser_seconds"] for record in records),
        "per_file": _summary([record["request_seconds"] for record in records]),
        "parser_total_seconds": sum(record["parser_seconds"] for record in records),
        "parser": _summary([record["parser_seconds"] for record in records]),
        "serialization": _summary([]),
        "result_readback": _summary([]),
        "serialization_total_seconds": 0.0,
        "result_readback_total_seconds": 0.0,
        "records": records,
        "results": results,
    }


def run_one_shot(paths: list[Path], adapter_name: str, timeout: float) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    results: list[list[list[Any]]] = []
    startup_values: list[float] = []
    shutdown_values: list[float] = []
    started = time.perf_counter()
    for index, path in enumerate(paths):
        file_started = time.perf_counter()
        worker = BenchmarkWorker("oneshot", timeout)
        startup_values.append(worker.start())
        try:
            measurement = worker.request(
                {"id": index, "op": "extract", "path": str(path), "adapter": adapter_name},
                timeout_seconds=timeout,
            )
            results.append(measurement.chunks)
            shutdown_values.append(worker.wait_for_exit())
        except BaseException:
            worker.terminate()
            raise
        records.append(
            {
                "path": path.name,
                "request_seconds": measurement.request_seconds,
                "write_seconds": measurement.write_seconds,
                "readback_seconds": measurement.readback_seconds,
                "parser_seconds": measurement.parser_seconds,
                "serialization_seconds": measurement.serialization_seconds,
                "one_shot_total_seconds": time.perf_counter() - file_started,
                "chunks": len(measurement.chunks),
            }
        )
    total = time.perf_counter() - started
    return {
        "mode": "one-shot",
        "files": len(paths),
        "total_seconds": total,
        "one_shot_total": total,
        "one_shot_total_seconds": total,
        "per_file": _summary([record["one_shot_total_seconds"] for record in records]),
        "first_request": _summary([record["request_seconds"] for record in records]),
        "parser_total_seconds": sum(record["parser_seconds"] for record in records),
        "parser": _summary([record["parser_seconds"] for record in records]),
        "serialization_total_seconds": sum(
            record["serialization_seconds"] for record in records
        ),
        "serialization": _summary(
            [record["serialization_seconds"] for record in records]
        ),
        "result_readback_total_seconds": sum(
            record["readback_seconds"] for record in records
        ),
        "result_readback": _summary([record["readback_seconds"] for record in records]),
        "worker_startup_total_seconds": sum(startup_values),
        "worker_startup": _summary(startup_values),
        "worker_shutdown_total_seconds": sum(shutdown_values),
        "worker_shutdown": _summary(shutdown_values),
        "records": records,
        "results": results,
    }


def run_persistent(paths: list[Path], adapter_name: str, timeout: float) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    results: list[list[list[Any]]] = []
    worker = BenchmarkWorker("persistent", timeout)
    started = time.perf_counter()
    startup_seconds = worker.start()
    try:
        for index, path in enumerate(paths):
            measurement = worker.request(
                {"id": index, "op": "extract", "path": str(path), "adapter": adapter_name},
                timeout_seconds=timeout,
            )
            results.append(measurement.chunks)
            records.append(
                {
                    "path": path.name,
                    "request_seconds": measurement.request_seconds,
                    "write_seconds": measurement.write_seconds,
                    "readback_seconds": measurement.readback_seconds,
                    "parser_seconds": measurement.parser_seconds,
                    "serialization_seconds": measurement.serialization_seconds,
                    "chunks": len(measurement.chunks),
                }
            )
        shutdown_seconds = worker.shutdown()
    except BaseException:
        worker.terminate()
        raise
    total = time.perf_counter() - started
    request_values = [record["request_seconds"] for record in records]
    steady_values = request_values[1:]
    return {
        "mode": "persistent",
        "files": len(paths),
        "total_seconds": total,
        "persistent_total": total,
        "persistent_total_seconds": total,
        "per_file": _summary(request_values),
        "persistent_first_file": request_values[0] * 1000 if request_values else None,
        "persistent_steady_state": _summary(steady_values),
        "steady_state_request": _summary(steady_values),
        "persistent_steady_p50_ms": _summary(steady_values)["p50_ms"],
        "persistent_steady_p95_ms": _summary(steady_values)["p95_ms"],
        "first_request": _summary(request_values[:1]),
        "parser_total_seconds": sum(record["parser_seconds"] for record in records),
        "parser": _summary([record["parser_seconds"] for record in records]),
        "serialization_total_seconds": sum(
            record["serialization_seconds"] for record in records
        ),
        "serialization": _summary(
            [record["serialization_seconds"] for record in records]
        ),
        "result_readback_total_seconds": sum(
            record["readback_seconds"] for record in records
        ),
        "result_readback": _summary([record["readback_seconds"] for record in records]),
        "worker_startup_total_seconds": startup_seconds,
        "worker_startup": _summary([startup_seconds]),
        "worker_shutdown_total_seconds": shutdown_seconds,
        "worker_shutdown": _summary([shutdown_seconds]),
        "respawn_total_seconds": 0.0,
        "respawn": {"total_seconds": 0.0},
        "records": records,
        "results": results,
    }


def run_reliability(path: Path, adapter_name: str, timeout: float) -> dict[str, Any]:
    """Exercise error continuation and kill/respawn behavior on one worker."""
    with tempfile.TemporaryDirectory(prefix="docseek-isolation-reliability-") as directory:
        root = Path(directory)
        broken = root / f"broken{path.suffix}"
        broken.write_bytes(b"not a valid office or pdf document")

        crash_worker = BenchmarkWorker("persistent", timeout)
        try:
            crash_worker.start()
            crash_worker.write_request({"id": 0, "op": "crash"})
            assert crash_worker.process is not None
            crash_worker.process.wait(timeout=HANG_TIMEOUT_SECONDS)
            worker_crash_detected = crash_worker.process.returncode == 23
            if not worker_crash_detected:
                raise AssertionError(
                    f"worker crash was not detected: returncode={crash_worker.process.returncode}"
                )
        except BaseException:
            crash_worker.terminate()
            raise

        worker = BenchmarkWorker("persistent", timeout)
        startup_seconds = worker.start()
        bad_file_error = ""
        try:
            try:
                worker.request(
                    {"id": 1, "op": "extract", "path": str(broken), "adapter": adapter_name},
                    timeout_seconds=timeout,
                )
            except WorkerError as exc:
                bad_file_error = str(exc)
            if not bad_file_error:
                raise AssertionError("broken file unexpectedly succeeded")

            good_after_error = worker.request(
                {"id": 2, "op": "extract", "path": str(path), "adapter": adapter_name},
                timeout_seconds=timeout,
            )
            good_chunks = good_after_error.chunks

            worker.write_request({"id": 3, "op": "hang"})
            hang_wait_started = time.perf_counter()
            while time.perf_counter() - hang_wait_started < HANG_TIMEOUT_SECONDS:
                if worker.process is None or worker.process.poll() is not None:
                    raise AssertionError("simulated hanging worker exited early")
                time.sleep(0.01)
            hang_timeout_observed = True
            kill_seconds = worker.terminate()

            respawn_started = time.perf_counter()
            replacement = BenchmarkWorker("persistent", timeout)
            replacement.start()
            replacement_result = replacement.request(
                {"id": 4, "op": "extract", "path": str(path), "adapter": adapter_name},
                timeout_seconds=timeout,
            )
            replacement.write_request({"id": 5, "op": "hang"})
            cancel_started = time.perf_counter()
            time.sleep(0.05)
            cancel_kill_seconds = replacement.terminate()
            cancel_latency_seconds = time.perf_counter() - cancel_started
            respawn_seconds = time.perf_counter() - respawn_started
        except BaseException:
            worker.terminate()
            raise

    if replacement_result.chunks != good_chunks:
        raise AssertionError("respawned worker returned different chunks")
    return {
        "bad_file_continues": True,
        "bad_file_error": bad_file_error,
        "worker_crash_detected": worker_crash_detected,
        "hang_timeout_observed": hang_timeout_observed,
        "hang_killed": True,
        "cancel_latency_ms": cancel_latency_seconds * 1000,
        "hang_kill_ms": kill_seconds * 1000,
        "cancel_kill_ms": cancel_kill_seconds * 1000,
        "respawn_seconds": respawn_seconds,
        "respawn": {"seconds": respawn_seconds},
        "initial_worker_startup_seconds": startup_seconds,
        "post_respawn_chunks": len(replacement_result.chunks),
    }


def _check_correctness(
    direct: dict[str, Any],
    one_shot: dict[str, Any] | None,
    persistent: dict[str, Any] | None,
) -> dict[str, bool]:
    expected = direct["results"]
    checks = {}
    if one_shot is not None:
        checks["direct_vs_one_shot"] = expected == one_shot["results"]
    if persistent is not None:
        checks["direct_vs_persistent"] = expected == persistent["results"]
    if not all(checks.values()):
        raise AssertionError(f"extraction output mismatch: {checks}")
    return checks


def _print_summary(report: dict[str, Any]) -> None:
    def format_ms(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.2f}"

    print("DocSeek extraction isolation benchmark")
    print(f"workload={report['workload']} files={report['files']:,}")
    print(f"fixture={report['fixture']}")
    print(f"adapter={report['adapter']}")
    for mode, result in report["modes"].items():
        print(
            f"{mode}: total={result['total_seconds']:.3f}s "
            f"per_file_p50={format_ms(result['per_file']['p50_ms'])}ms "
            f"per_file_p95={format_ms(result['per_file']['p95_ms'])}ms "
            f"parser_total={result['parser_total_seconds']:.3f}s "
            f"startup={result.get('worker_startup_total_seconds', 0.0):.3f}s "
            f"readback={result['result_readback_total_seconds']:.3f}s"
        )
        if mode == "persistent":
            steady = result["persistent_steady_state"]
            print(
                f"  first_file={format_ms(result['persistent_first_file'])}ms "
                f"steady_p50={format_ms(steady['p50_ms'])}ms "
                f"steady_p95={format_ms(steady['p95_ms'])}ms"
            )
    print("correctness=" + json.dumps(report["correctness"], ensure_ascii=False))
    print(
        "reliability="
        + json.dumps(report["reliability"], ensure_ascii=False, sort_keys=True)
    )


def _run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="docseek-isolation-benchmark-") as directory:
        root = Path(directory) / "documents"
        paths = _create_workload(root, args.workload, args.files)
        adapter_name = _select_adapter(paths[0])
        direct = run_direct(paths, adapter_name)
        one_shot = (
            run_one_shot(paths, adapter_name, args.timeout)
            if args.mode in {"one-shot", "all"}
            else None
        )
        persistent = (
            run_persistent(paths, adapter_name, args.timeout)
            if args.mode in {"persistent", "all"}
            else None
        )
        modes = {"direct": direct}
        if one_shot is not None:
            modes["one-shot"] = one_shot
        if persistent is not None:
            modes["persistent"] = persistent
        report = {
            "schema_version": 1,
            "benchmark": "extraction-isolation",
            "workload": args.workload,
            "files": args.files,
            "fixture": WORKLOADS[args.workload][1],
            "adapter": adapter_name,
            "mode": args.mode,
            "timeout_seconds": args.timeout,
            "platform": sys.platform,
            "python": sys.version,
            "modes": modes,
            "correctness": _check_correctness(direct, one_shot, persistent),
            "reliability": run_reliability(paths[0], adapter_name, args.timeout),
        }
        # Chunk payloads are useful for local diagnosis but make a 100-file
        # report needlessly large. Keep only counts in the saved artifact.
        for result in report["modes"].values():
            result.pop("results", None)
        return report


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="DocSeek parser versus process-isolation benchmark"
    )
    parser.add_argument("--workload", choices=tuple(WORKLOADS), default="docx")
    parser.add_argument("--files", type=int, default=None)
    parser.add_argument(
        "--mode",
        choices=("direct", "one-shot", "persistent", "all"),
        default="all",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    parser.add_argument("--worker", choices=("oneshot", "persistent"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return _worker_main(args.worker)
    if args.files is None:
        args.files = WORKLOADS[args.workload][2]
    if args.files < 1:
        parser.error("--files must be >= 1")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")

    report = _run_benchmark(args)
    _print_summary(report)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"report={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
