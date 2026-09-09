from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError as exc:  # pragma: no cover - exercised by the CLI boundary
    raise SystemExit(
        "benchmark_scan_backends.py requires psutil; install the benchmark extras "
        'with `python -m pip install -e ".[benchmarks]"`'
    ) from exc

from docseek.scan_backend import (
    MAX_SCAN_BATCH_SIZE,
    PythonScanBackend,
    RustScanBackend,
    ScanCancelled,
    ScanConfig,
)


SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_FILES = (10_000, 100_000)
DEFAULT_ITERATIONS = 3
DEFAULT_WARMUPS = 1
DEFAULT_CANCEL_ITERATIONS = 5
DEFAULT_CANCEL_WARMUPS = 1
DEFAULT_CANCEL_FILES = 100_000
DEFAULT_CANCEL_THRESHOLD = 1_000
DEFAULT_SAMPLE_INTERVAL_MS = 10
DEFAULT_WORKER_TIMEOUT_SECONDS = 900


def _count_label(count: int) -> str:
    if count % 1_000 == 0:
        return f"{count // 1_000}k"
    return str(count)


def _is_supported_index(index: int, scenario: str) -> bool:
    """Keep supported and unsupported entries deterministically interleaved."""
    return index % 10 != 0 if scenario == "dense" else index % 10 == 0


def _supported_count(files: int, scenario: str) -> int:
    return sum(_is_supported_index(index, scenario) for index in range(files))


def create_case(root: Path, files: int, scenario: str) -> int:
    """Create a flat, deterministic tree; creation time is outside the worker timer."""
    if root.exists():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    supported = _supported_count(files, scenario)
    for index in range(files):
        suffix = ".txt" if _is_supported_index(index, scenario) else ".bin"
        path = root / f"document_{index:06d}{suffix}"
        if suffix == ".txt":
            path.write_text("scan benchmark candidate\n", encoding="utf-8")
        else:
            path.touch()
    return supported


def _make_backend(name: str) -> Any:
    if name == "python":
        return PythonScanBackend()
    if name == "rust":
        # A benchmark must never silently measure Python after Rust startup
        # failure.  Strict mode makes an unavailable extension a hard error.
        return RustScanBackend(allow_fallback=False)
    raise ValueError(f"unsupported scan backend: {name}")


def _scan_config() -> ScanConfig:
    return ScanConfig(enabled_extensions={".txt"})


def _rss_mb(value: int) -> float:
    return value / 1_000_000


def _sample_peak_rss(
    process: psutil.Process,
    stop: threading.Event,
    peak_holder: list[int],
    interval_seconds: float,
) -> None:
    while not stop.wait(interval_seconds):
        try:
            peak_holder[0] = max(peak_holder[0], process.memory_info().rss)
        except psutil.Error:
            return


def measure_scan(root: Path, backend_name: str, expected_files: int) -> dict[str, Any]:
    process = psutil.Process()
    baseline_rss = process.memory_info().rss
    peak_holder = [baseline_rss]
    stop_sampler = threading.Event()
    sampler = threading.Thread(
        target=_sample_peak_rss,
        args=(process, stop_sampler, peak_holder, DEFAULT_SAMPLE_INTERVAL_MS / 1000),
        name="scan-benchmark-rss",
        daemon=True,
    )

    started = time.perf_counter()
    sampler.start()
    candidate_count = 0
    files_seen = 0
    first_progress_ms: float | None = None
    first_candidate_ms: float | None = None
    try:
        session = _make_backend(backend_name).start_scan(root, _scan_config())
        while True:
            batch = session.next_batch(MAX_SCAN_BATCH_SIZE)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if first_progress_ms is None and batch.progress.files_seen > 0:
                first_progress_ms = elapsed_ms
            if first_candidate_ms is None and batch.candidates:
                first_candidate_ms = elapsed_ms
            candidate_count += len(batch.candidates)
            files_seen = batch.progress.files_seen
            if batch.finished:
                break
    finally:
        stop_sampler.set()
        sampler.join(timeout=2)
        try:
            peak_holder[0] = max(peak_holder[0], process.memory_info().rss)
        except psutil.Error:
            pass

    total_ms = (time.perf_counter() - started) * 1000
    if files_seen != expected_files:
        raise RuntimeError(
            f"{backend_name} scanned {files_seen} files, expected {expected_files}"
        )

    return {
        "total_discovery_ms": total_ms,
        "files_seen": files_seen,
        "candidates_emitted": candidate_count,
        "files_per_second": files_seen / max(total_ms / 1000, 1e-9),
        "first_progress_ms": first_progress_ms,
        "first_candidate_ms": first_candidate_ms,
        "rss_baseline_mb": _rss_mb(baseline_rss),
        "rss_peak_mb": _rss_mb(peak_holder[0]),
        "rss_delta_mb": _rss_mb(max(0, peak_holder[0] - baseline_rss)),
    }


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def summarize_scan(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    numeric_metrics = (
        "total_discovery_ms",
        "files_seen",
        "candidates_emitted",
        "files_per_second",
        "first_progress_ms",
        "first_candidate_ms",
        "rss_baseline_mb",
        "rss_peak_mb",
        "rss_delta_mb",
    )
    summary: dict[str, Any] = {}
    for metric in numeric_metrics:
        values = [item[metric] for item in measurements if item[metric] is not None]
        summary[metric] = _median(values) if values else None
    return summary


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile without values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _cancel_once(root: Path, backend_name: str, threshold: int) -> dict[str, Any]:
    session = _make_backend(backend_name).start_scan(root, _scan_config())
    threshold_reached = threading.Event()
    files_seen_at_cancel_request = [0]
    outcome: dict[str, Any] = {}

    def scan_until_cancelled() -> None:
        try:
            while True:
                batch = session.next_batch(MAX_SCAN_BATCH_SIZE)
                if batch.progress.files_seen >= threshold:
                    files_seen_at_cancel_request[0] = batch.progress.files_seen
                    threshold_reached.set()
                if batch.finished:
                    outcome["completed"] = True
                    return
        except ScanCancelled:
            outcome["cancelled_at"] = time.perf_counter()
        except BaseException as exc:  # pragma: no cover - worker failure path
            outcome["error"] = repr(exc)

    scanner = threading.Thread(
        target=scan_until_cancelled,
        name=f"{backend_name}-scan-cancel-benchmark",
        daemon=True,
    )
    scanner.start()
    if not threshold_reached.wait(timeout=DEFAULT_WORKER_TIMEOUT_SECONDS):
        session.cancel()
        scanner.join(timeout=5)
        raise RuntimeError(
            f"{backend_name} did not reach files_seen >= {threshold} before timeout"
        )

    cancel_started = time.perf_counter()
    session.cancel()
    scanner.join(timeout=DEFAULT_WORKER_TIMEOUT_SECONDS)
    if scanner.is_alive():
        raise RuntimeError(f"{backend_name} did not stop after cancellation")
    if "error" in outcome:
        raise RuntimeError(f"{backend_name} cancel worker failed: {outcome['error']}")
    if "completed" in outcome:
        raise RuntimeError(
            f"{backend_name} completed before cancellation was observed"
        )
    cancelled_at = outcome.get("cancelled_at")
    if cancelled_at is None:
        raise RuntimeError(f"{backend_name} did not raise ScanCancelled")

    return {
        "cancel_latency_ms": (cancelled_at - cancel_started) * 1000,
        "files_seen_at_cancel_request": files_seen_at_cancel_request[0],
    }


def run_scan_worker(
    root: Path,
    backend_name: str,
    expected_files: int,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    for _ in range(warmups):
        measure_scan(root, backend_name, expected_files)
    measurements = [
        measure_scan(root, backend_name, expected_files)
        for _ in range(iterations)
    ]
    return {
        "operation": "scan",
        "backend": backend_name,
        "warmups": warmups,
        "iterations": iterations,
        "measurements": measurements,
        "summary": summarize_scan(measurements),
    }


def run_cancel_worker(
    root: Path,
    backend_name: str,
    threshold: int,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    for _ in range(warmups):
        _cancel_once(root, backend_name, threshold)
    measurements = [
        _cancel_once(root, backend_name, threshold) for _ in range(iterations)
    ]
    latencies = [item["cancel_latency_ms"] for item in measurements]
    return {
        "operation": "cancel",
        "backend": backend_name,
        "warmups": warmups,
        "iterations": iterations,
        "measurements": measurements,
        "summary": {
            "cancel_latency_ms_median": _median(latencies),
            "cancel_latency_ms_p95": _percentile(latencies, 0.95),
            "cancel_latency_ms_max": max(latencies),
        },
    }


def _run_worker(args: argparse.Namespace) -> None:
    if not args.root or not args.backend:
        raise SystemExit("worker mode requires --root and --backend")
    root = Path(args.root)
    if args.operation == "scan":
        result = run_scan_worker(
            root,
            args.backend,
            args.expected_files,
            args.warmups,
            args.iterations,
        )
    else:
        result = run_cancel_worker(
            root,
            args.backend,
            args.cancel_threshold,
            args.cancel_warmups,
            args.cancel_iterations,
        )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def _run_worker_process(
    *,
    root: Path,
    backend: str,
    operation: str,
    expected_files: int,
    iterations: int,
    warmups: int,
    cancel_threshold: int = DEFAULT_CANCEL_THRESHOLD,
    cancel_iterations: int = DEFAULT_CANCEL_ITERATIONS,
    cancel_warmups: int = DEFAULT_CANCEL_WARMUPS,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--worker",
        "--operation",
        operation,
        "--root",
        str(root),
        "--backend",
        backend,
        "--expected-files",
        str(expected_files),
        "--iterations",
        str(iterations),
        "--warmups",
        str(warmups),
        "--cancel-threshold",
        str(cancel_threshold),
        "--cancel-iterations",
        str(cancel_iterations),
        "--cancel-warmups",
        str(cancel_warmups),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=DEFAULT_WORKER_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{operation} worker failed for {backend} with exit code "
            f"{completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"{operation} worker produced no JSON for {backend}")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{operation} worker produced invalid JSON for {backend}: {completed.stdout}"
        ) from exc


def _format(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return f"{value:,}{suffix}"


def _rust_advantage(metric: str, python_value: Any, rust_value: Any) -> str:
    if python_value is None or rust_value is None or rust_value == 0:
        return "n/a"
    if metric == "files_per_second":
        ratio = rust_value / python_value if python_value else 0
    else:
        ratio = python_value / rust_value
    return f"{ratio:.2f}x"


def print_case_report(case_name: str, files: int, results: dict[str, Any]) -> None:
    print(f"\nCase: {case_name}")
    print(f"Files: {files:,}")
    for backend, result in results.items():
        summary = result["summary"]
        print(
            f"{backend:>6}: candidates={int(summary['candidates_emitted']):,} "
            f"median={summary['total_discovery_ms'] / 1000:.3f}s"
        )
    if not {"python", "rust"}.issubset(results):
        return

    python_summary = results["python"]["summary"]
    rust_summary = results["rust"]["summary"]
    print("\nMetric                    Python       Rust       Rust advantage")
    print("-----------------------------------------------------------------")
    rows = (
        ("Discovery", "total_discovery_ms", "ms"),
        ("Files/sec", "files_per_second", ""),
        ("First progress", "first_progress_ms", "ms"),
        ("First candidate", "first_candidate_ms", "ms"),
        ("Peak RSS delta", "rss_delta_mb", "MB"),
    )
    for label, metric, suffix in rows:
        python_value = python_summary[metric]
        rust_value = rust_summary[metric]
        print(
            f"{label:<25} {_format(python_value, suffix):>10} "
            f"{_format(rust_value, suffix):>10} "
            f"{_rust_advantage(metric, python_value, rust_value):>15}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare DocSeek Python and Rust scanner-only performance"
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--operation", choices=("scan", "cancel"), default="scan")
    parser.add_argument("--root", help=argparse.SUPPRESS)
    parser.add_argument("--backend", choices=("python", "rust", "both"), default="both")
    parser.add_argument(
        "--files",
        type=int,
        nargs="+",
        default=list(DEFAULT_FILES),
        help="file counts to benchmark (default: 10000 100000)",
    )
    parser.add_argument(
        "--scenario",
        choices=("sparse", "dense", "both"),
        default="both",
        help="file composition to benchmark",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help="timed scan iterations per backend",
    )
    parser.add_argument(
        "--warmups",
        type=int,
        default=DEFAULT_WARMUPS,
        help="scan warmups per backend",
    )
    parser.add_argument("--json-out", default="benchmark-results.json")
    parser.add_argument("--skip-cancel", action="store_true")
    parser.add_argument("--expected-files", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--cancel-threshold", type=int, default=DEFAULT_CANCEL_THRESHOLD)
    parser.add_argument(
        "--cancel-iterations",
        type=int,
        default=DEFAULT_CANCEL_ITERATIONS,
        help="timed cancellation iterations per backend",
    )
    parser.add_argument(
        "--cancel-warmups",
        type=int,
        default=DEFAULT_CANCEL_WARMUPS,
        help="cancellation warmups per backend",
    )
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args()
    if args.worker:
        _run_worker(args)
        return

    if any(files < 10 for files in args.files):
        raise SystemExit("--files values must be >= 10")
    if args.iterations < 1 or args.warmups < 0:
        raise SystemExit("--iterations must be >= 1 and --warmups must be >= 0")
    if args.cancel_iterations < 1 or args.cancel_warmups < 0:
        raise SystemExit(
            "--cancel-iterations must be >= 1 and --cancel-warmups must be >= 0"
        )
    if args.cancel_threshold < 1:
        raise SystemExit("--cancel-threshold must be >= 1")

    scenarios = ("sparse", "dense") if args.scenario == "both" else (args.scenario,)
    backends = ("python", "rust") if args.backend == "both" else (args.backend,)
    report: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "DocSeek Scan Backend Benchmark",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version,
            "cpu_count": os.cpu_count(),
            "psutil_version": psutil.__version__,
        },
        "parameters": {
            "files": args.files,
            "scenarios": list(scenarios),
            "backends": list(backends),
            "warmups": args.warmups,
            "iterations": args.iterations,
            "cancel_threshold": args.cancel_threshold,
            "cancel_warmups": args.cancel_warmups,
            "cancel_iterations": args.cancel_iterations,
            "rss_sample_interval_ms": DEFAULT_SAMPLE_INTERVAL_MS,
            "filesystem_cache_mode": "warm",
        },
        "cases": [],
        "cancellation": [],
    }

    print("DocSeek Scan Backend Benchmark")
    for files in args.files:
        for scenario in scenarios:
            case_name = f"{scenario}-{_count_label(files)}"
            with tempfile.TemporaryDirectory(prefix="docseek-scan-benchmark-") as temp_dir:
                root = Path(temp_dir) / "documents"
                expected_candidates = create_case(root, files, scenario)
                results: dict[str, Any] = {}
                for backend in backends:
                    result = _run_worker_process(
                        root=root,
                        backend=backend,
                        operation="scan",
                        expected_files=files,
                        iterations=args.iterations,
                        warmups=args.warmups,
                    )
                    for measurement in result["measurements"]:
                        if measurement["candidates_emitted"] != expected_candidates:
                            raise RuntimeError(
                                f"{case_name}/{backend} emitted "
                                f"{measurement['candidates_emitted']} candidates, "
                                f"expected {expected_candidates}"
                            )
                    results[backend] = result
            print_case_report(case_name, files, results)
            report["cases"].append(
                {
                    "name": case_name,
                    "files": files,
                    "scenario": scenario,
                    "expected_candidates": expected_candidates,
                    "backends": results,
                }
            )

    if not args.skip_cancel:
        cancel_files = max(DEFAULT_CANCEL_FILES, max(args.files))
        cancel_threshold = min(args.cancel_threshold, cancel_files - 1)
        with tempfile.TemporaryDirectory(prefix="docseek-scan-cancel-") as temp_dir:
            root = Path(temp_dir) / "documents"
            create_case(root, cancel_files, "sparse")
            for backend in backends:
                result = _run_worker_process(
                    root=root,
                    backend=backend,
                    operation="cancel",
                    expected_files=cancel_files,
                    iterations=args.iterations,
                    warmups=args.warmups,
                    cancel_threshold=cancel_threshold,
                    cancel_iterations=args.cancel_iterations,
                    cancel_warmups=args.cancel_warmups,
                )
                report["cancellation"].append(
                    {
                        "name": f"cancel-sparse-{_count_label(cancel_files)}",
                        "files": cancel_files,
                        "backend": backend,
                        "threshold": cancel_threshold,
                        **result,
                    }
                )
        print("\nCancellation latency (sparse workload)")
        for result in report["cancellation"]:
            summary = result["summary"]
            print(
                f"{result['backend']:>6}: median={summary['cancel_latency_ms_median']:.2f}ms "
                f"p95={summary['cancel_latency_ms_p95']:.2f}ms "
                f"max={summary['cancel_latency_ms_max']:.2f}ms"
            )

    json_path = Path(args.json_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nJSON report: {json_path}")


if __name__ == "__main__":
    main()
