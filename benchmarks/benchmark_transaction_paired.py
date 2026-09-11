from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import psutil

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

from benchmark_scan import WORKLOADS  # noqa: E402
from benchmark_transaction_sweep import print_case, run_case  # noqa: E402
import docseek.indexer as indexer_module  # noqa: E402


CAPACITIES = (128, 512)
DEFAULT_REPEATS = 2
DEFAULT_CANCEL_FILES = 2_000
PRODUCTION_TRANSACTION_CAPACITY = 512

MEDIAN_METRICS = (
    "first_scan_seconds",
    "writer_total_seconds",
    "sql_total_seconds",
    "commit_total_seconds",
    "scanner_wait_seconds",
    "cjk_tokenize_seconds",
    "fts_normal_seconds",
    "fts_cjk_seconds",
    "rss_peak_mb",
    "rss_delta_mb",
    "cancel_latency_ms",
)


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _delta_percent(baseline: float, candidate: float) -> float:
    if baseline == 0.0:
        return 0.0 if candidate == 0.0 else float("inf")
    return (candidate - baseline) / baseline * 100.0


def _metric(result: dict[str, Any], name: str) -> float:
    if name == "cancel_latency_ms":
        return float(result["cancel"]["latency_ms"])
    return float(result[name])


def _runner_snapshot() -> dict[str, Any]:
    process = psutil.Process(os.getpid())
    return {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory_total_mb": psutil.virtual_memory().total / 1024 / 1024,
        "process_start_rss_mb": process.memory_info().rss / 1024 / 1024,
    }


def _capacity_summary(
    results: list[dict[str, Any]],
    capacity: int,
) -> dict[str, Any]:
    selected = [item for item in results if item["transaction_capacity"] == capacity]
    if not selected:
        raise RuntimeError(f"no results for capacity {capacity}")

    summary: dict[str, Any] = {
        "capacity": capacity,
        "repeats": len(selected),
        "transaction_counts": sorted(
            {int(item["transaction_count"]) for item in selected}
        ),
        "commit_counts": sorted({int(item["commit_count"]) for item in selected}),
        "metrics": {
            name: _median([_metric(item, name) for item in selected])
            for name in MEDIAN_METRICS
        },
        "correctness": all(
            item["indexed"] == item["files_requested"]
            and item["chunks"] == item["validation"]["chunks"]
            and item["validation"]["integrity"] == "ok"
            and item["validation"] == item["reopen_validation"]
            and item["cancel"]["integrity"] == "ok"
            for item in selected
        ),
    }
    return summary


def _paired_metric_summary(
    results: list[dict[str, Any]],
    metric_name: str,
) -> dict[str, Any]:
    by_repeat = {
        int(item["repeat"]): item
        for item in results
        if item["transaction_capacity"] == 128
    }
    paired: list[dict[str, float]] = []
    for item in results:
        if item["transaction_capacity"] != 512:
            continue
        repeat = int(item["repeat"])
        baseline = by_repeat.get(repeat)
        if baseline is None:
            raise RuntimeError(f"missing paired 128 result for repeat {repeat}")
        baseline_value = _metric(baseline, metric_name)
        candidate_value = _metric(item, metric_name)
        paired.append(
            {
                "repeat": repeat,
                "baseline_128": baseline_value,
                "candidate_512": candidate_value,
                "delta_percent": _delta_percent(baseline_value, candidate_value),
                "speedup": (
                    baseline_value / candidate_value if candidate_value else 0.0
                ),
            }
        )
    if not paired:
        raise RuntimeError(f"no paired results for {metric_name}")
    deltas = [item["delta_percent"] for item in paired]
    return {
        "samples": paired,
        "median_delta_percent": _median(deltas),
        "speedup_median": _median([item["speedup"] for item in paired]),
        "candidate_not_slower_count": sum(
            item["candidate_512"] <= item["baseline_128"] for item in paired
        ),
    }


def summarize_group(
    *,
    workload: str,
    backend: str,
    files: int,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    oracle = results[0]["validation"]
    for item in results:
        if item["validation"] != oracle:
            raise RuntimeError(
                f"{workload}/{backend}/capacity={item['transaction_capacity']} "
                "database/search validation diverged from the paired oracle"
            )
    baseline = _capacity_summary(results, 128)
    candidate = _capacity_summary(results, 512)
    deltas = {
        name: _delta_percent(
            baseline["metrics"][name], candidate["metrics"][name]
        )
        for name in MEDIAN_METRICS
    }
    return {
        "workload": workload,
        "backend": backend,
        "files": files,
        "baseline_128": baseline,
        "candidate_512": candidate,
        "delta_percent_512_vs_128": deltas,
        "speedup_128_over_512": {
            name: (
                baseline["metrics"][name] / candidate["metrics"][name]
                if candidate["metrics"][name]
                else 0.0
            )
            for name in MEDIAN_METRICS
        },
        "paired": {
            name: _paired_metric_summary(results, name)
            for name in (
                "first_scan_seconds",
                "writer_total_seconds",
                "commit_total_seconds",
            )
        },
        "correctness": bool(baseline["correctness"] and candidate["correctness"]),
    }


def _format_delta(value: float) -> str:
    if value == float("inf"):
        return "+inf"
    return f"{value:+.1f}%"


def print_group_summary(summary: dict[str, Any]) -> None:
    baseline = summary["baseline_128"]
    candidate = summary["candidate_512"]
    deltas = summary["delta_percent_512_vs_128"]
    paired = summary["paired"]["first_scan_seconds"]
    print(
        "summary="
        f"{summary['workload']} backend={summary['backend']} "
        f"repeats={candidate['repeats']} correctness={summary['correctness']} "
        f"first_scan_128_median={baseline['metrics']['first_scan_seconds']:.3f}s "
        f"first_scan_512_median={candidate['metrics']['first_scan_seconds']:.3f}s "
        f"delta={_format_delta(deltas['first_scan_seconds'])} "
        f"speedup={summary['speedup_128_over_512']['first_scan_seconds']:.3f}x "
        f"paired_delta_median={_format_delta(paired['median_delta_percent'])} "
        f"512_not_slower={paired['candidate_not_slower_count']}/{len(paired['samples'])} "
        f"commit_128={baseline['metrics']['commit_total_seconds']:.3f}s "
        f"commit_512={candidate['metrics']['commit_total_seconds']:.3f}s "
        f"commit_delta={_format_delta(deltas['commit_total_seconds'])} "
        f"transactions={baseline['transaction_counts']}->"
        f"{candidate['transaction_counts']} "
        f"rss_peak={candidate['metrics']['rss_peak_mb']:.1f}MiB "
        f"cancel={candidate['metrics']['cancel_latency_ms']:.1f}ms"
    )


def build_conclusion(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    first_scan_deltas = [
        item["delta_percent_512_vs_128"]["first_scan_seconds"]
        for item in summaries
    ]
    commit_deltas = [
        item["delta_percent_512_vs_128"]["commit_total_seconds"]
        for item in summaries
    ]
    transaction_reduced = all(
        max(item["candidate_512"]["transaction_counts"])
        < min(item["baseline_128"]["transaction_counts"])
        for item in summaries
    )
    cancel_ok = all(
        item["candidate_512"]["metrics"]["cancel_latency_ms"] < 200.0
        for item in summaries
    )
    return {
        "all_correctness_ok": all(item["correctness"] for item in summaries),
        "transaction_count_reduced": transaction_reduced,
        "all_median_first_scan_non_regression": all(
            delta <= 0.0 for delta in first_scan_deltas
        ),
        "any_median_first_scan_regression": any(
            delta > 0.0 for delta in first_scan_deltas
        ),
        "all_median_commit_non_regression": all(
            delta <= 0.0 for delta in commit_deltas
        ),
        "cancel_under_200ms": cancel_ok,
        "first_scan_delta_range_percent": [
            min(first_scan_deltas),
            max(first_scan_deltas),
        ],
        "commit_delta_range_percent": [min(commit_deltas), max(commit_deltas)],
        "recommendation": (
            "close M9-B and proceed to M9-C profiling"
            if all(item["correctness"] for item in summaries)
            and transaction_reduced
            and not any(delta > 0.0 for delta in first_scan_deltas)
            else "keep 512 under review; repeat or investigate paired regressions"
        ),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Same-run paired SQLite transaction boundary benchmark"
    )
    parser.add_argument("--workload", choices=tuple(WORKLOADS), action="append")
    parser.add_argument(
        "--files",
        type=int,
        default=None,
        help="override the file count for every selected workload (useful for smoke runs)",
    )
    parser.add_argument("--backend", choices=("python", "rust", "both"), default="both")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--cancel-files", type=int, default=DEFAULT_CANCEL_FILES)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()

    if args.repeats < 2:
        parser.error("--repeats must be >= 2 for paired confirmation")
    if args.cancel_files < 1:
        parser.error("--cancel-files must be >= 1")
    if args.files is not None and args.files < 1:
        parser.error("--files must be >= 1")
    if int(indexer_module.FULL_SCAN_BATCH_SIZE) != PRODUCTION_TRANSACTION_CAPACITY:
        raise RuntimeError(
            "production FULL_SCAN_BATCH_SIZE must remain 512; "
            f"got {indexer_module.FULL_SCAN_BATCH_SIZE}"
        )

    workload_names = args.workload or ["tiny-text", "medium-text", "large-text"]
    default_files = {
        "tiny-text": 50_000,
        "medium-text": 10_000,
        "large-text": 1_000,
    }
    backend_names = ("python", "rust") if args.backend == "both" else (args.backend,)
    runner = _runner_snapshot()
    raw_results: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    sequence = 0

    print("DocSeek M9-B.2 paired SQLite transaction A/B")
    print(
        "benchmark-only overrides: production FULL_SCAN_BATCH_SIZE=512; "
        "SQLite PRAGMA, journal mode, synchronous, FTS and tokenizer unchanged"
    )
    print(
        f"workloads={workload_names} backends={backend_names} "
        f"repeats={args.repeats} capacity_order=128,512"
    )
    print(f"runner={json.dumps(runner, ensure_ascii=False, sort_keys=True)}")

    for workload_name in workload_names:
        workload = WORKLOADS[workload_name]
        files = args.files if args.files is not None else default_files.get(workload_name, 100)
        for repeat in range(args.repeats):
            # Keep every backend in the same capacity pair, while alternating
            # backend order between repeats to reduce systematic order bias.
            backend_order = (
                backend_names if repeat % 2 == 0 else tuple(reversed(backend_names))
            )
            for capacity in CAPACITIES:
                for backend_name in backend_order:
                    sequence += 1
                    print(
                        f"run={sequence} workload={workload_name} files={files} "
                        f"repeat={repeat + 1}/{args.repeats} backend={backend_name} "
                        f"capacity={capacity}"
                    )
                    result = run_case(
                        files=files,
                        workload=workload,
                        backend_name=backend_name,
                        capacity_label=str(capacity),
                        capacity=capacity,
                        cancel_files=args.cancel_files,
                    )
                    result["repeat"] = repeat
                    result["sequence"] = sequence
                    result["production_capacity"] = PRODUCTION_TRANSACTION_CAPACITY
                    print_case(result)
                    raw_results.append(result)
                    grouped[(workload_name, backend_name)].append(result)
                    if int(indexer_module.FULL_SCAN_BATCH_SIZE) != PRODUCTION_TRANSACTION_CAPACITY:
                        raise RuntimeError(
                            "benchmark override did not restore production capacity"
                        )

    # The transaction-capacity oracle above validates each backend separately.
    # This second oracle keeps the existing Python/Rust full-index parity
    # contract explicit for every workload in the paired experiment.
    for workload_name in workload_names:
        workload_results = [
            item for item in raw_results if item["workload"] == workload_name
        ]
        oracle = workload_results[0]["validation"]
        for item in workload_results:
            if item["validation"] != oracle:
                raise RuntimeError(
                    f"{workload_name}/{item['backend']}/capacity="
                    f"{item['transaction_capacity']} failed Python/Rust "
                    "database/search parity"
                )

    summaries = [
        summarize_group(
            workload=workload_name,
            backend=backend_name,
            files=(
                args.files
                if args.files is not None
                else default_files.get(workload_name, 100)
            ),
            results=results,
        )
        for (workload_name, backend_name), results in grouped.items()
    ]
    for summary in summaries:
        print_group_summary(summary)
    conclusion = build_conclusion(summaries)
    print(f"conclusion={json.dumps(conclusion, ensure_ascii=False, sort_keys=True)}")

    report = {
        "benchmark": "m9-b.2-paired-transaction-ab",
        "production_transaction_capacity": PRODUCTION_TRANSACTION_CAPACITY,
        "capacities": list(CAPACITIES),
        "workloads": workload_names,
        "backends": backend_names,
        "repeats": args.repeats,
        "runner": runner,
        "results": raw_results,
        "summaries": summaries,
        "conclusion": conclusion,
    }
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"json_report={args.json_out}")
    if args.report_out is not None:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "DocSeek M9-B.2 paired SQLite transaction A/B",
            f"runner={json.dumps(runner, ensure_ascii=False, sort_keys=True)}",
            "",
        ]
        lines.extend(
            f"{summary['workload']} backend={summary['backend']} "
            f"first_scan_128={summary['baseline_128']['metrics']['first_scan_seconds']:.3f}s "
            f"first_scan_512={summary['candidate_512']['metrics']['first_scan_seconds']:.3f}s "
            f"delta={_format_delta(summary['delta_percent_512_vs_128']['first_scan_seconds'])} "
            f"speedup={summary['speedup_128_over_512']['first_scan_seconds']:.3f}x "
            f"commit_delta={_format_delta(summary['delta_percent_512_vs_128']['commit_total_seconds'])} "
            f"transactions={summary['baseline_128']['transaction_counts']}->"
            f"{summary['candidate_512']['transaction_counts']} "
            f"rss_peak_512={summary['candidate_512']['metrics']['rss_peak_mb']:.1f}MiB "
            f"cancel_512={summary['candidate_512']['metrics']['cancel_latency_ms']:.1f}ms "
            f"correctness={summary['correctness']}"
            for summary in summaries
        )
        lines.extend(
            [
                "",
                f"conclusion={json.dumps(conclusion, ensure_ascii=False, sort_keys=True)}",
            ]
        )
        args.report_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"text_report={args.report_out}")


if __name__ == "__main__":
    main()
