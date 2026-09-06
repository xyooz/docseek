from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import docseek.indexer as indexer_module
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase


def create_files(root: Path, count: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"document_{index:06d}.txt").write_text(
            f"客户经理 信贷 业务制度 文件 {index}\n第二行办公资料",
            encoding="utf-8",
        )


def timed_scan(
    indexer: DirectoryIndexer,
    root: Path,
    *,
    profile_phases: bool = False,
) -> tuple[float, float, int, object, dict[str, float]]:
    started = time.perf_counter()
    discovery_seconds: float | None = None
    candidate_count = 0
    phase = "discovery"
    timings: dict[str, float] = {}

    def add_timing(name: str, elapsed: float) -> None:
        timings[name] = timings.get(name, 0.0) + elapsed

    def timed_call(name: str, func: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            call_started = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                add_timing(name, time.perf_counter() - call_started)

        return wrapped

    restorers: list[Callable[[], None]] = []
    if profile_phases:
        original_normalize = indexer._normalize

        def timed_normalize(path: Path) -> str:
            call_started = time.perf_counter()
            try:
                return original_normalize(path)
            finally:
                add_timing(
                    "normalize_discovery" if phase == "discovery" else "normalize_post",
                    time.perf_counter() - call_started,
                )

        indexer._normalize = timed_normalize  # type: ignore[method-assign]
        restorers.append(
            lambda: setattr(indexer, "_normalize", original_normalize)
        )

        original_index_existing = indexer._index_existing_file
        indexer._index_existing_file = timed_call(  # type: ignore[method-assign]
            "candidate_check", original_index_existing
        )
        restorers.append(
            lambda: setattr(indexer, "_index_existing_file", original_index_existing)
        )

        original_clear_many = indexer.issues.clear_many
        indexer.issues.clear_many = timed_call(  # type: ignore[method-assign]
            "issue_clear_many", original_clear_many
        )
        restorers.append(
            lambda: setattr(indexer.issues, "clear_many", original_clear_many)
        )

        original_record_many = indexer.issues.record_many
        indexer.issues.record_many = timed_call(  # type: ignore[method-assign]
            "issue_record_many", original_record_many
        )
        restorers.append(
            lambda: setattr(indexer.issues, "record_many", original_record_many)
        )

        original_clear_stale = indexer.issues.clear_under_root_if_missing
        indexer.issues.clear_under_root_if_missing = timed_call(  # type: ignore[method-assign]
            "issue_stale_cleanup", original_clear_stale
        )
        restorers.append(
            lambda: setattr(
                indexer.issues,
                "clear_under_root_if_missing",
                original_clear_stale,
            )
        )

        original_remove_missing = indexer_module.remove_missing_under_root
        indexer_module.remove_missing_under_root = timed_call(
            "missing_cleanup", original_remove_missing
        )
        restorers.append(
            lambda: setattr(
                indexer_module,
                "remove_missing_under_root",
                original_remove_missing,
            )
        )

    def candidates_ready(count: int) -> None:
        nonlocal discovery_seconds, candidate_count, phase
        if discovery_seconds is None:
            discovery_seconds = time.perf_counter() - started
        candidate_count = count
        phase = "post"

    try:
        stats = indexer.scan(root, on_candidates_ready=candidates_ready)
    finally:
        for restore in reversed(restorers):
            restore()

    total_seconds = time.perf_counter() - started
    return total_seconds, discovery_seconds or 0.0, candidate_count, stats, timings


def format_phase_timings(timings: dict[str, float]) -> str:
    if not timings:
        return ""
    order = (
        "normalize_discovery",
        "normalize_post",
        "candidate_check",
        "issue_clear_many",
        "issue_record_many",
        "missing_cleanup",
        "issue_stale_cleanup",
    )
    return " ".join(
        f"{name}={timings.get(name, 0.0):.3f}s"
        for name in order
        if name in timings
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DocSeek directory scan benchmark")
    parser.add_argument("--files", type=int, default=1000, help="number of small text files")
    args = parser.parse_args()
    if args.files < 1:
        parser.error("--files must be >= 1")

    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        root = base / "documents"
        db = SearchDatabase(base / "docseek.db")
        create_files(root, args.files)

        (
            first_seconds,
            first_discovery_seconds,
            first_candidates,
            first_stats,
            _first_timings,
        ) = timed_scan(DirectoryIndexer(db), root)
        (
            second_seconds,
            second_discovery_seconds,
            second_candidates,
            second_stats,
            second_timings,
        ) = timed_scan(DirectoryIndexer(db), root, profile_phases=True)

        changed = root / "document_000000.txt"
        changed.write_text("客户经理 信贷 精准增量更新后的内容", encoding="utf-8")

        init_started = time.perf_counter()
        incremental_indexer = DirectoryIndexer(db)
        init_ms = (time.perf_counter() - init_started) * 1000

        update_started = time.perf_counter()
        update_stats = incremental_indexer.update_paths([changed])
        update_only_ms = (time.perf_counter() - update_started) * 1000
        total_update_ms = init_ms + update_only_ms

        first_post_discovery = max(0.0, first_seconds - first_discovery_seconds)
        second_post_discovery = max(0.0, second_seconds - second_discovery_seconds)

        print("DocSeek directory scan benchmark")
        print(f"files={args.files:,}")
        print(
            f"first_scan={first_seconds:.3f}s "
            f"discovery={first_discovery_seconds:.3f}s "
            f"post_discovery={first_post_discovery:.3f}s "
            f"candidates={first_candidates:,} "
            f"discovery_share={first_discovery_seconds / first_seconds:.1%} "
            f"files_per_second={args.files / first_seconds:.1f} "
            f"indexed={first_stats.indexed}"
        )
        print(
            f"unchanged_scan={second_seconds:.3f}s "
            f"discovery={second_discovery_seconds:.3f}s "
            f"post_discovery={second_post_discovery:.3f}s "
            f"candidates={second_candidates:,} "
            f"discovery_share={second_discovery_seconds / second_seconds:.1%} "
            f"files_per_second={args.files / second_seconds:.1f} "
            f"unchanged={second_stats.unchanged}"
        )
        if second_timings:
            measured = sum(
                value
                for name, value in second_timings.items()
                if name not in {"normalize_discovery"}
            )
            residual = max(0.0, second_post_discovery - measured)
            normalize_post = second_timings.get("normalize_post", 0.0)
            missing_cleanup = second_timings.get("missing_cleanup", 0.0)
            hot_share = (
                (normalize_post + missing_cleanup) / second_post_discovery
                if second_post_discovery > 0
                else 0.0
            )
            print(
                "unchanged_phases "
                + format_phase_timings(second_timings)
                + f" residual_post={residual:.3f}s"
                + f" path_hot_share={hot_share:.1%}"
            )
        print(
            f"single_file_total={total_update_ms:.2f}ms "
            f"indexer_init={init_ms:.2f}ms update_only={update_only_ms:.2f}ms "
            f"indexed={update_stats.indexed} removed={update_stats.removed}"
        )


if __name__ == "__main__":
    main()
