from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import psutil

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

from benchmark_scan import WORKLOADS, create_workload  # noqa: E402
from docseek.chunk_writer import ChunkBatchWriter  # noqa: E402
from docseek.indexer import DirectoryIndexer, IndexCancelled  # noqa: E402
from docseek.persistent_extraction import PersistentExtractionWorker  # noqa: E402
from docseek.scan_backend import PythonScanBackend, RustScanBackend  # noqa: E402
from docseek.search_db import SearchDatabase  # noqa: E402


PRODUCTION_TRANSACTION_CAPACITY = 512
INDEXER_PHASES = (
    "scanner",
    "index_loop",
    "writer_batch",
    "writer_flush",
)
CONTROL_PHASES = ("sqlite_execute", "sqlite_commit")
ALL_PHASES = INDEXER_PHASES + CONTROL_PHASES


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


class Trace:
    """Monotonic event trace for one deterministic cancellation sample."""

    def __init__(self, phase: str, backend: str) -> None:
        self.phase = phase
        self.backend = backend
        self.started = time.perf_counter()
        self.events: dict[str, float] = {}
        self.event_lock = threading.Lock()

    def mark(self, name: str) -> float:
        value = time.perf_counter() - self.started
        with self.event_lock:
            self.events.setdefault(name, value)
        return value

    def has(self, name: str) -> bool:
        return name in self.events

    def at(self, name: str) -> float | None:
        return self.events.get(name)

    def duration(self, start: str, end: str) -> float | None:
        first = self.at(start)
        last = self.at(end)
        if first is None or last is None:
            return None
        return max(0.0, (last - first) * 1000.0)

    def metrics(self) -> dict[str, float | None]:
        return {
            "request_to_cancel_call_ms": self.duration(
                "target_phase_entered", "cancel_called"
            ),
            "cancel_call_duration_ms": self.duration(
                "cancel_called", "cancel_call_returned"
            ),
            "cancel_to_observed_ms": self.duration(
                "cancel_called", "cancellation_observed"
            ),
            "cancel_to_index_thread_exit_ms": self.duration(
                "cancel_called", "index_thread_exit"
            ),
            "cancel_to_cleanup_ms": self.duration(
                "cancel_called", "cleanup_complete"
            ),
            # This intentionally excludes the deterministic Event wait used to
            # hold the target phase. It measures runner scheduling after the
            # cancellation thread has been released by the phase barrier.
            "runner_scheduling_gap_ms": self.duration(
                "cancel_thread_ready", "cancel_called"
            ),
            "native_call_duration_ms": self.duration(
                "sqlite_native_entered", "sqlite_call_returned"
            ),
            "commit_duration_ms": self.duration(
                "sqlite_commit_native_entered", "sqlite_commit_native_returned"
            ),
        }

    def result(self, *, error: str | None = None) -> dict[str, Any]:
        native_entered = self.at("sqlite_native_entered") or self.at(
            "sqlite_commit_native_entered"
        )
        native_returned = self.at("sqlite_call_returned") or self.at(
            "sqlite_commit_native_returned"
        )
        cancel_called = self.at("cancel_called")
        return {
            "phase": self.phase,
            "backend": self.backend,
            "events_ms": dict(self.events),
            "metrics_ms": self.metrics(),
            "native_call_overlap": (
                native_entered is not None
                and native_returned is not None
                and cancel_called is not None
                and native_entered <= cancel_called <= native_returned
            ),
            "persistent_worker_process_used": False,
            "error": error,
        }


class PhaseGate:
    """A barrier released only by the cancellation path, never by sleep."""

    def __init__(self, trace: Trace) -> None:
        self.trace = trace
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self) -> None:
        self.trace.mark("target_phase_entered")
        self.entered.set()
        if not self.release.wait(timeout=60.0):
            raise TimeoutError("cancellation phase barrier was not released")

    def open(self) -> None:
        self.trace.mark("phase_barrier_released")
        self.release.set()


class TracedEvent:
    """Delegate threading.Event while exposing the Python cancel transition."""

    def __init__(self, inner: threading.Event, trace: Trace) -> None:
        self._inner = inner
        self._trace = trace

    def set(self) -> None:
        self._trace.mark("python_cancel_event_set")
        self._inner.set()

    def clear(self) -> None:
        self._inner.clear()

    def is_set(self) -> bool:
        return self._inner.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._inner.wait(timeout)


class GatedScanSession:
    def __init__(self, inner: Any, gate: PhaseGate, trace: Trace) -> None:
        self._inner = inner
        self._gate = gate
        self._trace = trace
        self._entered = False

    def next_batch(self, max_items: int = 128) -> Any:
        if not self._entered:
            self._entered = True
            self._gate.wait()
        return self._inner.next_batch(max_items)

    def cancel(self) -> Any:
        self._trace.mark("scanner_cancel_invoked")
        try:
            return self._inner.cancel()
        finally:
            self._trace.mark("scanner_cancel_returned")
            self._gate.open()

    def snapshot(self) -> Any:
        return self._inner.snapshot()

    def is_finished(self) -> bool:
        return self._inner.is_finished()


class GatedScanBackend:
    def __init__(self, inner: Any, gate: PhaseGate, trace: Trace) -> None:
        self._inner = inner
        self._gate = gate
        self._trace = trace
        self.scan_backend_name = getattr(inner, "scan_backend_name", "custom")
        self.allow_fallback = getattr(inner, "allow_fallback", False)

    def start_scan(self, root: Path, config: Any = None) -> GatedScanSession:
        return GatedScanSession(
            self._inner.start_scan(root, config), self._gate, self._trace
        )


def backend_factory(name: str) -> Callable[[], Any]:
    if name == "python":
        return PythonScanBackend
    if name == "rust":
        return lambda: RustScanBackend(allow_fallback=False)
    raise ValueError(f"unknown backend: {name}")


def _patch_persistent_worker_trace(trace: Trace) -> list[tuple[Any, str, Any]]:
    restorers: list[tuple[Any, str, Any]] = []
    original_cancel = PersistentExtractionWorker.cancel
    original_close = PersistentExtractionWorker.close

    def traced_cancel(worker: PersistentExtractionWorker) -> bool:
        trace.mark("persistent_worker_cancel_invoked")
        try:
            return original_cancel(worker)
        finally:
            trace.mark("persistent_worker_cancel_returned")

    def traced_close(worker: PersistentExtractionWorker) -> None:
        try:
            return original_close(worker)
        finally:
            trace.mark("cleanup_complete")

    PersistentExtractionWorker.cancel = traced_cancel  # type: ignore[method-assign]
    PersistentExtractionWorker.close = traced_close  # type: ignore[method-assign]
    restorers.extend(
        [
            (PersistentExtractionWorker, "cancel", original_cancel),
            (PersistentExtractionWorker, "close", original_close),
        ]
    )
    return restorers


def _restore(restorers: list[tuple[Any, str, Any]]) -> None:
    for target, name, original in reversed(restorers):
        setattr(target, name, original)


def run_indexer_phase(*, phase: str, backend_name: str) -> dict[str, Any]:
    trace = Trace(phase, backend_name)
    gate = PhaseGate(trace)
    restorers: list[tuple[Any, str, Any]] = []
    outcome: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="docseek-cancel-boundary-") as temp:
        base = Path(temp)
        root = base / "documents"
        # The production scan has an early-commit checkpoint at 16 indexed
        # files. Use a few more files for the flush boundary so the hook is
        # guaranteed to sit in front of a real batch commit; the other phases
        # stay deliberately tiny.
        create_workload(
            root,
            80 if phase == "writer_flush" else 8,
            WORKLOADS["tiny-text"],
        )
        database = SearchDatabase(base / "docseek.db")
        delegate = backend_factory(backend_name)()
        if phase == "scanner":
            delegate = GatedScanBackend(delegate, gate, trace)
        indexer = DirectoryIndexer(database, scan_backend=delegate)
        indexer._cancel = TracedEvent(indexer._cancel, trace)  # type: ignore[assignment]
        original_cancel = indexer.cancel

        def traced_indexer_cancel() -> None:
            trace.mark("cancel_called")
            try:
                original_cancel()
            finally:
                trace.mark("cancel_call_returned")
                gate.open()

        indexer.cancel = traced_indexer_cancel  # type: ignore[method-assign]
        restorers.extend([(indexer, "cancel", original_cancel)])
        restorers.extend(_patch_persistent_worker_trace(trace))

        if phase == "index_loop":
            original_index = indexer._index_existing_file

            def gated_index(*args: Any, **kwargs: Any) -> Any:
                gate.wait()
                if trace.has("cancel_called"):
                    raise IndexCancelled()
                return original_index(*args, **kwargs)

            indexer._index_existing_file = gated_index  # type: ignore[method-assign]
            restorers.append((indexer, "_index_existing_file", original_index))

        if phase == "writer_batch":
            original_replace = ChunkBatchWriter.replace_document
            call_counts: defaultdict[int, int] = defaultdict(int)

            def gated_replace(writer: ChunkBatchWriter, *args: Any, **kwargs: Any) -> Any:
                key = id(writer)
                call_counts[key] += 1
                if call_counts[key] == 2:
                    gate.wait()
                    if trace.has("cancel_called"):
                        raise IndexCancelled()
                return original_replace(writer, *args, **kwargs)

            ChunkBatchWriter.replace_document = gated_replace  # type: ignore[method-assign]
            restorers.append((ChunkBatchWriter, "replace_document", original_replace))

        if phase == "writer_flush":
            original_flush = ChunkBatchWriter.flush
            triggered: set[int] = set()

            def gated_flush(writer: ChunkBatchWriter, *args: Any, **kwargs: Any) -> Any:
                key = id(writer)
                if writer._transaction_open and key not in triggered:
                    triggered.add(key)
                    gate.wait()
                    trace.mark("sqlite_native_entered")
                    try:
                        result = original_flush(writer, *args, **kwargs)
                    finally:
                        trace.mark("sqlite_call_returned")
                    if trace.has("cancel_called"):
                        raise IndexCancelled()
                    return result
                return original_flush(writer, *args, **kwargs)

            ChunkBatchWriter.flush = gated_flush  # type: ignore[method-assign]
            restorers.append((ChunkBatchWriter, "flush", original_flush))

        scan_thread = threading.Thread(target=_scan_thread, args=(indexer, root, outcome, trace), daemon=True)
        scan_thread.start()
        if not gate.entered.wait(timeout=60.0):
            scan_thread.join(timeout=5.0)
            _restore(restorers)
            return trace.result(error="target phase was not reached")

        trace.mark("cancel_thread_scheduled")

        def cancel_thread() -> None:
            if not gate.entered.wait(timeout=60.0):
                return
            trace.mark("cancel_thread_ready")
            indexer.cancel()

        stopper = threading.Thread(target=cancel_thread, daemon=True)
        stopper.start()
        stopper.join(timeout=60.0)
        scan_thread.join(timeout=60.0)
        if scan_thread.is_alive():
            trace.mark("forced_join_timeout")
            outcome["error"] = "index thread did not exit after cancellation"
        if stopper.is_alive():
            outcome["error"] = "cancel thread did not exit"
        _restore(restorers)

    if "error" not in outcome and not trace.has("cancellation_observed"):
        outcome["error"] = f"index outcome was not cancellation: {outcome.get('exception')}"
    result = trace.result(error=outcome.get("error"))
    result["outcome"] = outcome
    return result


def _scan_thread(
    indexer: DirectoryIndexer,
    root: Path,
    outcome: dict[str, Any],
    trace: Trace,
) -> None:
    try:
        outcome["stats"] = indexer.scan(root)
    except IndexCancelled:
        trace.mark("cancellation_observed")
        outcome["cancelled"] = True
    except BaseException as exc:  # noqa: BLE001 - diagnostic records the failure
        trace.mark("index_thread_error")
        outcome["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        trace.mark("index_thread_exit")


def run_sqlite_execute_phase() -> dict[str, Any]:
    trace = Trace("sqlite_execute", "control")
    entered = threading.Event()
    release = threading.Event()
    cancel_event = threading.Event()
    outcome: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="docseek-sqlite-execute-") as temp:
        path = Path(temp) / "probe.db"

        def cancel_gate() -> int:
            trace.mark("target_phase_entered")
            trace.mark("sqlite_native_entered")
            entered.set()
            if not release.wait(timeout=60.0):
                raise TimeoutError("execute barrier was not released")
            if cancel_event.is_set():
                trace.mark("cancellation_observed")
            return 1

        # The probe deliberately runs the SQLite call on a worker thread and
        # drives cancellation from another thread. This is a benchmark-only
        # cross-thread connection; production connections remain unchanged.
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.create_function("cancel_gate", 0, cancel_gate)

        def execute_thread() -> None:
            try:
                conn.execute("SELECT cancel_gate()").fetchone()
                trace.mark("sqlite_call_returned")
            except BaseException as exc:  # noqa: BLE001
                outcome["exception"] = f"{type(exc).__name__}: {exc}"
            finally:
                trace.mark("index_thread_exit")

        worker = threading.Thread(target=execute_thread, daemon=True)
        worker.start()
        if not entered.wait(timeout=60.0):
            outcome["error"] = "sqlite execute phase was not reached"
        else:
            trace.mark("cancel_thread_scheduled")

            def cancel() -> None:
                trace.mark("cancel_thread_ready")
                trace.mark("cancel_called")
                cancel_event.set()
                trace.mark("python_cancel_event_set")
                release.set()
                trace.mark("cancel_call_returned")

            stopper = threading.Thread(target=cancel, daemon=True)
            stopper.start()
            stopper.join(timeout=60.0)
            worker.join(timeout=60.0)
            if worker.is_alive():
                outcome["error"] = "sqlite execute thread did not exit"
        conn.close()
        trace.mark("cleanup_complete")

    result = trace.result(error=outcome.get("error"))
    result["outcome"] = outcome
    return result


def run_sqlite_commit_phase(*, rows: int, payload_bytes: int) -> dict[str, Any]:
    trace = Trace("sqlite_commit", "control")
    entered = threading.Event()
    outcome: dict[str, Any] = {}

    class CommitProbeConnection(sqlite3.Connection):
        def commit(self) -> None:
            trace.mark("sqlite_commit_native_entered")
            entered.set()
            try:
                return super().commit()
            finally:
                trace.mark("sqlite_commit_native_returned")

    with tempfile.TemporaryDirectory(prefix="docseek-sqlite-commit-") as temp:
        path = Path(temp) / "probe.db"
        conn = sqlite3.connect(
            path,
            factory=CommitProbeConnection,
            check_same_thread=False,
        )
        conn.execute("CREATE TABLE payload(value BLOB NOT NULL)")
        conn.commit()
        trace.events.clear()
        payload = b"x" * payload_bytes
        conn.execute("BEGIN IMMEDIATE")
        for _ in range(rows):
            conn.execute("INSERT INTO payload(value) VALUES (?)", (payload,))
        trace.mark("target_phase_entered")

        def commit_thread() -> None:
            try:
                trace.mark("sqlite_call_started")
                conn.commit()
                trace.mark("sqlite_call_returned")
                if trace.has("cancel_called"):
                    # sqlite3.commit() has no Python cancellation callback.
                    # If Stop was requested while the native call was active,
                    # the first observable boundary is its return.
                    trace.mark("cancellation_observed")
            except BaseException as exc:  # noqa: BLE001
                outcome["exception"] = f"{type(exc).__name__}: {exc}"
            finally:
                trace.mark("index_thread_exit")

        worker = threading.Thread(target=commit_thread, daemon=True)
        worker.start()
        trace.mark("cancel_thread_scheduled")

        def cancel() -> None:
            if not entered.wait(timeout=60.0):
                return
            trace.mark("cancel_thread_ready")
            trace.mark("cancel_called")
            trace.mark("python_cancel_event_set")
            trace.mark("cancel_call_returned")

        stopper = threading.Thread(target=cancel, daemon=True)
        stopper.start()
        stopper.join(timeout=60.0)
        worker.join(timeout=60.0)
        if worker.is_alive():
            outcome["error"] = "sqlite commit thread did not exit"
        conn.close()
        trace.mark("cleanup_complete")

    result = trace.result(error=outcome.get("error"))
    result["outcome"] = outcome
    result["commit_rows"] = rows
    result["commit_payload_bytes"] = payload_bytes
    return result


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = (
        "request_to_cancel_call_ms",
        "cancel_call_duration_ms",
        "cancel_to_observed_ms",
        "cancel_to_index_thread_exit_ms",
        "cancel_to_cleanup_ms",
        "runner_scheduling_gap_ms",
        "native_call_duration_ms",
        "commit_duration_ms",
    )
    summary: dict[str, Any] = {
        "samples": len(samples),
        "errors": sum(bool(sample.get("error")) for sample in samples),
        "native_call_overlap_count": sum(
            bool(sample.get("native_call_overlap")) for sample in samples
        ),
        "metrics": {},
    }
    for name in metrics:
        values = [
            float(sample["metrics_ms"][name])
            for sample in samples
            if sample["metrics_ms"].get(name) is not None
        ]
        summary["metrics"][name] = {
            "count": len(values),
            "median": statistics.median(values) if values else None,
            "p95": percentile(values, 0.95) if values else None,
            "max": max(values, default=None),
        }
    return summary


def print_summary(key: str, summary: dict[str, Any]) -> None:
    metrics = summary["metrics"]
    def value(name: str, field: str) -> str:
        raw = metrics[name][field]
        return "n/a" if raw is None else f"{raw:.1f}ms"

    print(
        f"summary={key} samples={summary['samples']} errors={summary['errors']} "
        f"native_overlap={summary['native_call_overlap_count']} "
        f"cancel_to_observed={value('cancel_to_observed_ms', 'median')}/"
        f"{value('cancel_to_observed_ms', 'p95')}/"
        f"{value('cancel_to_observed_ms', 'max')} "
        f"cancel_to_exit={value('cancel_to_index_thread_exit_ms', 'median')}/"
        f"{value('cancel_to_index_thread_exit_ms', 'p95')}/"
        f"{value('cancel_to_index_thread_exit_ms', 'max')} "
        f"cancel_to_cleanup={value('cancel_to_cleanup_ms', 'median')}/"
        f"{value('cancel_to_cleanup_ms', 'p95')}/"
        f"{value('cancel_to_cleanup_ms', 'max')}"
    )


def runner_snapshot() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory_total_mb": psutil.virtual_memory().total / 1024 / 1024,
        "process_rss_mb": psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Benchmark-only cancellation boundary diagnosis"
    )
    parser.add_argument("--phase", choices=ALL_PHASES, action="append")
    parser.add_argument("--backend", choices=("python", "rust", "both"), default="both")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--commit-rows", type=int, default=256)
    parser.add_argument("--commit-payload-bytes", type=int, default=32 * 1024)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    if args.commit_rows < 1 or args.commit_payload_bytes < 1:
        parser.error("commit probe sizes must be positive")
    if int(__import__("docseek.indexer", fromlist=["FULL_SCAN_BATCH_SIZE"]).FULL_SCAN_BATCH_SIZE) != PRODUCTION_TRANSACTION_CAPACITY:
        raise RuntimeError("production transaction capacity is not 512")

    phases = args.phase or list(ALL_PHASES)
    backends = ("python", "rust") if args.backend == "both" else (args.backend,)
    raw: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    print(
        "DocSeek M9-B.3 cancellation boundary diagnosis "
        f"production_transaction_capacity={PRODUCTION_TRANSACTION_CAPACITY} "
        f"repeats={args.repeats}"
    )
    print(f"runner={json.dumps(runner_snapshot(), ensure_ascii=True)}")

    for phase in phases:
        if phase in INDEXER_PHASES:
            for backend in backends:
                for repeat in range(1, args.repeats + 1):
                    print(f"run phase={phase} backend={backend} repeat={repeat}/{args.repeats}")
                    sample = run_indexer_phase(phase=phase, backend_name=backend)
                    sample["repeat"] = repeat
                    raw.append(sample)
                    grouped[f"{phase}/{backend}"].append(sample)
        elif phase == "sqlite_execute":
            for repeat in range(1, args.repeats + 1):
                print(f"run phase={phase} repeat={repeat}/{args.repeats}")
                sample = run_sqlite_execute_phase()
                sample["repeat"] = repeat
                raw.append(sample)
                grouped[phase].append(sample)
        elif phase == "sqlite_commit":
            for repeat in range(1, args.repeats + 1):
                print(f"run phase={phase} repeat={repeat}/{args.repeats}")
                sample = run_sqlite_commit_phase(
                    rows=args.commit_rows,
                    payload_bytes=args.commit_payload_bytes,
                )
                sample["repeat"] = repeat
                raw.append(sample)
                grouped[phase].append(sample)

    summaries = {key: summarize(samples) for key, samples in grouped.items()}
    for key, summary in summaries.items():
        print_summary(key, summary)

    conclusion = {
        "production_transaction_capacity": PRODUCTION_TRANSACTION_CAPACITY,
        "all_samples_error_free": all(not item.get("error") for item in raw),
        "native_overlap_by_group": {
            key: summary["native_call_overlap_count"] for key, summary in summaries.items()
        },
        "interpretation": {
            "request_to_cancel_call": "phase barrier to cancel() entry",
            "cancel_call_duration": "cancel() entry to return",
            "cancel_to_observed": "cancel() entry to target component observing cancellation",
            "cancel_to_index_thread_exit": "cancel() entry to worker/index thread exit",
            "runner_scheduling_gap": "cancel thread released to cancel() entry",
            "native_call_overlap": "cancel() called before the instrumented native call returned",
            "persistent_worker": "not used by the text-indexer phases; recorded as false",
        },
    }
    report = {
        "schema_version": 1,
        "runner": runner_snapshot(),
        "production_transaction_capacity": PRODUCTION_TRANSACTION_CAPACITY,
        "phases": phases,
        "repeats": args.repeats,
        "backend": args.backend,
        "commit_probe": {
            "rows": args.commit_rows,
            "payload_bytes": args.commit_payload_bytes,
            "sqlite_pragmas_changed": False,
        },
        "summaries": summaries,
        "conclusion": conclusion,
        "samples": raw,
    }
    if args.json_out:
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8"
        )
    if args.report_out:
        args.report_out.write_text(
            json.dumps(
                {"summaries": summaries, "conclusion": conclusion},
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )
    if not conclusion["all_samples_error_free"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
