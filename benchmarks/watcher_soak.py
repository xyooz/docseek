from __future__ import annotations

import argparse
import json
import queue
import random
import shutil
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.file_exclusions import FileExclusionStore
from docseek.index_issues import IndexIssueStore
from docseek.indexer import DirectoryIndexer
from docseek.search_db import SearchDatabase
from docseek.watcher import WatchBatch, WatchManager


class SoakFailure(RuntimeError):
    pass


@dataclass(slots=True)
class SoakCounters:
    batches: int = 0
    precise_batches: int = 0
    precise_paths: int = 0
    full_rescans: int = 0
    max_settle_seconds: float = 0.0


def _write_marker(path: Path, marker: str, *, padding: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{marker}\n客户经理 信贷 稳定性测试 " + ("X" * max(1, padding)),
        encoding="utf-8",
    )


def _indexed_content(database: SearchDatabase) -> dict[str, str]:
    store = ChunkStore(database.db_path)
    content_by_path: dict[str, list[str]] = {}
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT f.path, c.content
            FROM files f
            JOIN chunks c ON c.file_id = f.id
            ORDER BY f.path, c.ordinal
            """
        ).fetchall()
    for row in rows:
        content_by_path.setdefault(str(row["path"]), []).append(
            store.decode_content(row["content"])
        )
    return {path: "\n".join(parts) for path, parts in content_by_path.items()}


def verify_index_state(
    database: SearchDatabase,
    expected: dict[Path, str],
) -> None:
    actual = _indexed_content(database)
    expected_by_path = {str(path.resolve()): marker for path, marker in expected.items()}

    actual_paths = set(actual)
    expected_paths = set(expected_by_path)
    missing = sorted(expected_paths - actual_paths)
    stale = sorted(actual_paths - expected_paths)
    if missing or stale:
        raise SoakFailure(
            "索引路径与磁盘状态不一致："
            f" missing={missing[:5]} stale={stale[:5]}"
        )

    stale_content: list[str] = []
    for path, marker in expected_by_path.items():
        if marker not in actual.get(path, ""):
            stale_content.append(path)
    if stale_content:
        raise SoakFailure(
            "文件路径存在但正文没有更新到最新版本："
            + ", ".join(stale_content[:5])
        )

    issue_count = IndexIssueStore(database.db_path).count()
    if issue_count:
        raise SoakFailure(f"稳定性测试产生了 {issue_count} 个未恢复索引问题")


def _apply_batch(
    database: SearchDatabase,
    root: Path,
    batch: WatchBatch,
    counters: SoakCounters,
) -> None:
    counters.batches += 1
    if batch.full_rescan:
        DirectoryIndexer(database).scan(root)
        counters.full_rescans += 1
        return

    paths = [Path(path) for path in batch.paths]
    if paths:
        DirectoryIndexer(database).update_paths(paths)
        counters.precise_batches += 1
        counters.precise_paths += len(paths)


def settle_watcher(
    event_queue: queue.Queue[WatchBatch],
    *,
    database: SearchDatabase,
    root: Path,
    counters: SoakCounters,
    debounce_seconds: float,
    event_timeout_seconds: float,
) -> float:
    """Wait until at least one watcher batch arrives and the queue becomes quiet."""
    started = time.perf_counter()
    deadline = started + event_timeout_seconds
    quiet_window = max(0.20, debounce_seconds * 2.5)
    received = False

    while True:
        now = time.perf_counter()
        if not received and now >= deadline:
            raise SoakFailure(
                f"{event_timeout_seconds:.1f}s 内未收到 watcher 事件"
            )
        wait_for = quiet_window if received else max(0.01, deadline - now)
        try:
            batch = event_queue.get(timeout=wait_for)
        except queue.Empty:
            if received:
                break
            continue

        received = True
        _apply_batch(database, root, batch, counters)

    elapsed = time.perf_counter() - started
    counters.max_settle_seconds = max(counters.max_settle_seconds, elapsed)
    return elapsed


def _choose_existing(expected: dict[Path, str], rng: random.Random) -> Path | None:
    if not expected:
        return None
    return rng.choice(sorted(expected, key=lambda path: str(path)))


def mutate_cycle(
    cycle: int,
    *,
    root: Path,
    expected: dict[Path, str],
    rng: random.Random,
) -> str:
    operation = cycle % 6

    if operation == 0 or not expected:
        path = root / f"new_{cycle:06d}.txt"
        marker = f"DOCSEEK_SOAK_CREATE_{cycle:06d}"
        _write_marker(path, marker, padding=cycle % 31 + 1)
        expected[path] = marker
        return f"create {path.name}"

    target = _choose_existing(expected, rng)
    assert target is not None

    if operation == 1:
        marker = f"DOCSEEK_SOAK_MODIFY_{cycle:06d}"
        _write_marker(target, marker, padding=cycle % 37 + 17)
        expected[target] = marker
        return f"modify {target.name}"

    if operation == 2:
        renamed = target.with_name(f"renamed_{cycle:06d}_{target.name}")
        target.rename(renamed)
        marker = expected.pop(target)
        expected[renamed] = marker
        return f"rename {target.name} -> {renamed.name}"

    if operation == 3:
        target.unlink()
        expected.pop(target)
        return f"delete {target.name}"

    if operation == 4:
        ignored = root / f"ignore_{cycle:06d}.txt"
        _write_marker(
            ignored,
            f"DOCSEEK_SOAK_IGNORED_{cycle:06d}",
            padding=cycle % 19 + 3,
        )
        return f"excluded-create {ignored.name}"

    destination_dir = root / f"group_{cycle:06d}"
    destination_dir.mkdir(parents=True, exist_ok=False)
    moved = destination_dir / target.name
    target.rename(moved)
    marker = expected.pop(target)
    expected[moved] = marker
    return f"directory-move {target.name} -> {destination_dir.name}/"


def run_soak(
    *,
    session_dir: Path,
    cycles: int,
    initial_files: int,
    debounce_seconds: float,
    event_timeout_seconds: float,
    interval_seconds: float,
    seed: int,
    progress_every: int,
) -> dict[str, object]:
    documents = session_dir / "documents"
    documents.mkdir(parents=True, exist_ok=False)
    database = SearchDatabase(session_dir / "docseek.db")
    FileExclusionStore(database).set_patterns(["ignore_*"])

    expected: dict[Path, str] = {}
    for index in range(initial_files):
        path = documents / f"initial_{index:04d}.txt"
        marker = f"DOCSEEK_SOAK_INITIAL_{index:04d}"
        _write_marker(path, marker, padding=index + 1)
        expected[path] = marker

    # Verify file exclusion behavior during the initial reconciliation as well.
    ignored_seed = documents / "ignore_seed.txt"
    _write_marker(ignored_seed, "DOCSEEK_SOAK_IGNORE_SEED", padding=5)

    initial_stats = DirectoryIndexer(database).scan(documents)
    verify_index_state(database, expected)

    batches: queue.Queue[WatchBatch] = queue.Queue()
    manager = WatchManager(batches.put, debounce_seconds=debounce_seconds)
    counters = SoakCounters()
    rng = random.Random(seed)

    tracemalloc.start()
    started = time.perf_counter()
    try:
        manager.start([str(documents)], database.get_excluded_paths())
        # Give the native observer thread a small readiness window before the
        # first mutation. This is not part of the measured convergence latency.
        time.sleep(max(0.20, debounce_seconds * 2.0))

        for cycle in range(cycles):
            operation = mutate_cycle(
                cycle,
                root=documents,
                expected=expected,
                rng=rng,
            )
            settle = settle_watcher(
                batches,
                database=database,
                root=documents,
                counters=counters,
                debounce_seconds=debounce_seconds,
                event_timeout_seconds=event_timeout_seconds,
            )
            verify_index_state(database, expected)

            if progress_every > 0 and (
                (cycle + 1) % progress_every == 0 or cycle + 1 == cycles
            ):
                print(
                    f"cycle={cycle + 1}/{cycles} operation={operation} "
                    f"files={len(expected)} settle={settle:.3f}s "
                    f"batches={counters.batches} full_rescans={counters.full_rescans}"
                )
            if interval_seconds > 0:
                time.sleep(interval_seconds)
    finally:
        manager.stop()
        elapsed = time.perf_counter() - started
        current_memory, peak_memory = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    verify_index_state(database, expected)
    return {
        "cycles": cycles,
        "initial_files": initial_files,
        "final_indexed_files": len(expected),
        "initial_indexed": initial_stats.indexed,
        "initial_excluded": initial_stats.excluded,
        "batches": counters.batches,
        "precise_batches": counters.precise_batches,
        "precise_paths": counters.precise_paths,
        "full_rescans": counters.full_rescans,
        "max_settle_seconds": round(counters.max_settle_seconds, 4),
        "elapsed_seconds": round(elapsed, 3),
        "python_current_memory_mib": round(current_memory / (1024 * 1024), 3),
        "python_peak_memory_mib": round(peak_memory / (1024 * 1024), 3),
        "seed": seed,
        "status": "pass",
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=(
            "DocSeek watcher/indexer soak test. It only mutates a dedicated "
            "docseek-soak-* child directory created for the test."
        )
    )
    parser.add_argument("--cycles", type=int, default=120)
    parser.add_argument("--initial-files", type=int, default=12)
    parser.add_argument("--debounce", type=float, default=0.10)
    parser.add_argument("--event-timeout", type=float, default=5.0)
    parser.add_argument(
        "--interval",
        type=float,
        default=0.05,
        help="seconds to sleep between cycles; increase this for long wall-clock soaks",
    )
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="optional parent directory/drive to exercise; a unique child directory is created",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the generated session directory after a successful run",
    )
    args = parser.parse_args()

    if args.cycles < 1:
        parser.error("--cycles must be >= 1")
    if args.initial_files < 1:
        parser.error("--initial-files must be >= 1")
    if args.debounce <= 0 or args.event_timeout <= 0 or args.interval < 0:
        parser.error("debounce/event-timeout must be > 0 and interval must be >= 0")

    if args.workspace is None:
        session_dir = Path(tempfile.mkdtemp(prefix="docseek-soak-"))
    else:
        parent = args.workspace.expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        session_dir = Path(
            tempfile.mkdtemp(prefix="docseek-soak-", dir=str(parent))
        )

    succeeded = False
    try:
        result = run_soak(
            session_dir=session_dir,
            cycles=args.cycles,
            initial_files=args.initial_files,
            debounce_seconds=args.debounce,
            event_timeout_seconds=args.event_timeout,
            interval_seconds=args.interval,
            seed=args.seed,
            progress_every=args.progress_every,
        )
        succeeded = True
        print("DocSeek watcher/indexer soak result")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"SOAK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"diagnostic_session={session_dir}", file=sys.stderr)
        raise
    finally:
        if succeeded and not args.keep:
            shutil.rmtree(session_dir, ignore_errors=True)
        elif succeeded:
            print(f"kept_session={session_dir}")


if __name__ == "__main__":
    main()
