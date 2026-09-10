from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import psutil

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

from benchmark_scan import WORKLOADS, WorkloadSpec, create_workload, timed_scan  # noqa: E402
from docseek.chunk_codec import decode_chunk_content  # noqa: E402
from docseek.chunk_store import ChunkStore  # noqa: E402
import docseek.indexer as indexer_module  # noqa: E402
from docseek.indexer import DirectoryIndexer, IndexCancelled  # noqa: E402
from docseek.scan_backend import PythonScanBackend, RustScanBackend  # noqa: E402
from docseek.search_db import SearchDatabase  # noqa: E402


DEFAULT_CAPACITY_TOKENS = ("128", "256", "512", "1024", "files")
DEFAULT_CANCEL_FILES = 2_000
SEARCH_QUERIES = ("客户经理", "信贷")


class RssSampler:
    """Benchmark-only process RSS sampler for one isolated scan case."""

    def __init__(self, interval_seconds: float = 0.05) -> None:
        self._process = psutil.Process(os.getpid())
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.start_bytes = 0
        self.peak_bytes = 0

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)
            except psutil.Error:
                return
            self._stop.wait(self._interval_seconds)

    def __enter__(self) -> "RssSampler":
        self.start_bytes = self._process.memory_info().rss
        self.peak_bytes = self.start_bytes
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)
        except psutil.Error:
            pass

    @property
    def start_mb(self) -> float:
        return self.start_bytes / 1024 / 1024

    @property
    def peak_mb(self) -> float:
        return self.peak_bytes / 1024 / 1024

    @property
    def delta_mb(self) -> float:
        return max(0.0, self.peak_mb - self.start_mb)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def parse_capacity(token: str, files: int) -> tuple[str, int]:
    normalized = token.strip().casefold()
    if normalized in {"files", "single", "single-large-transaction"}:
        return "single", files
    capacity = int(normalized)
    if capacity < 1:
        raise ValueError("transaction capacity must be greater than zero")
    return str(capacity), capacity


def _relative_path(root: Path, value: object) -> str:
    try:
        canonical_root = os.path.realpath(os.fspath(root))
        canonical_value = os.path.realpath(str(value))
        return os.path.relpath(canonical_value, canonical_root)
    except ValueError:
        return str(value)


def _update_digest(digest: "hashlib._Hash", *values: object) -> None:
    for value in values:
        encoded = str(value).encode("utf-8", errors="surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)


def database_snapshot(db_path: Path, root: Path) -> dict[str, Any]:
    """Return a stable content/search oracle, excluding run-specific mtimes."""

    store = ChunkStore(db_path)
    file_digest = hashlib.sha256()
    chunk_digest = hashlib.sha256()
    state_digest = hashlib.sha256()
    with store.connect() as conn:
        files_count = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        chunks_count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        normal_fts_count = int(conn.execute("SELECT COUNT(*) FROM chunk_index").fetchone()[0])
        cjk_fts_count = int(conn.execute("SELECT COUNT(*) FROM chunk_index_cjk2").fetchone()[0])
        state_count = int(
            conn.execute("SELECT COUNT(*) FROM extraction_state").fetchone()[0]
        )
        for row in conn.execute(
            "SELECT path, filename, extension, size, last_error FROM files ORDER BY path"
        ):
            _update_digest(
                file_digest,
                _relative_path(root, row[0]),
                row[1],
                row[2],
                row[3],
                row[4],
            )
        for row in conn.execute(
            """
            SELECT f.path, c.ordinal, c.location, c.content
            FROM chunks c JOIN files f ON f.id = c.file_id
            ORDER BY f.path, c.ordinal
            """
        ):
            _update_digest(
                chunk_digest,
                _relative_path(root, row[0]),
                row[1],
                row[2],
                decode_chunk_content(row[3]),
            )
        for row in conn.execute(
            """
            SELECT path, revision, status, source_size, failure_count
            FROM extraction_state ORDER BY path
            """
        ):
            _update_digest(
                state_digest,
                _relative_path(root, row[0]),
                row[1],
                row[2],
                row[3],
                row[4],
            )
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])

    search = {
        query: sorted(
            (
                _relative_path(root, result.path),
                result.filename,
                result.location,
            )
            for result in store.search(query, limit=100)
        )
        for query in SEARCH_QUERIES
    }
    return {
        "files": files_count,
        "chunks": chunks_count,
        "normal_fts": normal_fts_count,
        "cjk_fts": cjk_fts_count,
        "extraction_state": state_count,
        "files_digest": file_digest.hexdigest(),
        "chunks_digest": chunk_digest.hexdigest(),
        "state_digest": state_digest.hexdigest(),
        "search": search,
        "integrity": integrity,
    }


def assert_database_parity(
    before_reopen: dict[str, Any],
    after_reopen: dict[str, Any],
    *,
    label: str,
) -> None:
    if before_reopen != after_reopen:
        raise RuntimeError(f"{label} changed after database reopen")
    if before_reopen["integrity"] != "ok":
        raise RuntimeError(f"{label} integrity_check={before_reopen['integrity']!r}")


def run_cancel_probe(
    *,
    files: int,
    workload: WorkloadSpec,
    backend_name: str,
    transaction_capacity: int,
) -> dict[str, Any]:
    backend_factory: Callable[[], object] = {
        "python": PythonScanBackend,
        "rust": lambda: RustScanBackend(allow_fallback=False),
    }[backend_name]
    with tempfile.TemporaryDirectory(prefix="docseek-cancel-") as temp:
        base = Path(temp)
        root = base / "documents"
        create_workload(root, files, workload)
        database = SearchDatabase(base / "docseek.db")
        indexer = DirectoryIndexer(database, scan_backend=backend_factory())
        ready = threading.Event()
        outcome: dict[str, Any] = {}

        def on_progress(_path: Path, stats: Any) -> None:
            outcome["indexed_before_cancel"] = int(getattr(stats, "indexed", 0))
            if int(getattr(stats, "indexed", 0)) >= min(250, max(1, files // 4)):
                ready.set()

        def scan_thread() -> None:
            try:
                outcome["stats"] = indexer.scan(root, on_progress=on_progress)
            except BaseException as exc:  # noqa: BLE001 - probe records the outcome
                outcome["error"] = exc

        original_scan_batch_size = indexer_module.MAX_SCAN_BATCH_SIZE
        indexer_module.MAX_SCAN_BATCH_SIZE = transaction_capacity
        try:
            worker = threading.Thread(target=scan_thread, daemon=True)
            worker.start()
            if not ready.wait(timeout=120):
                indexer.cancel()
                worker.join(timeout=10)
                raise RuntimeError(
                    f"{backend_name}/capacity={transaction_capacity} cancel probe never reached its checkpoint"
                )
            requested_at = time.perf_counter()
            indexer.cancel()
            worker.join(timeout=120)
            cancel_latency_ms = (time.perf_counter() - requested_at) * 1000
            if worker.is_alive():
                raise RuntimeError(
                    f"{backend_name}/capacity={transaction_capacity} cancel probe did not stop"
                )
            error = outcome.get("error")
            if not isinstance(error, IndexCancelled):
                raise RuntimeError(
                    f"{backend_name}/capacity={transaction_capacity} cancel returned {error!r}"
                )
        finally:
            indexer_module.MAX_SCAN_BATCH_SIZE = original_scan_batch_size

        reopened = database_snapshot(database.db_path, root)
        if reopened["integrity"] != "ok":
            raise RuntimeError(
                f"{backend_name}/capacity={transaction_capacity} cancel left an invalid database"
            )
        if not (
            reopened["files"] == reopened["chunks"]
            == reopened["normal_fts"]
            == reopened["cjk_fts"]
        ):
            raise RuntimeError(
                f"{backend_name}/capacity={transaction_capacity} cancel left mismatched "
                "files/chunks/FTS rows"
            )
        return {
            "latency_ms": cancel_latency_ms,
            "indexed_before_cancel": outcome.get("indexed_before_cancel"),
            "files_after_cancel": reopened["files"],
            "chunks_after_cancel": reopened["chunks"],
            "integrity": reopened["integrity"],
        }


def run_case(
    *,
    files: int,
    workload: WorkloadSpec,
    backend_name: str,
    capacity_label: str,
    capacity: int,
    cancel_files: int,
) -> dict[str, Any]:
    backend_factory: Callable[[], object] = {
        "python": PythonScanBackend,
        "rust": lambda: RustScanBackend(allow_fallback=False),
    }[backend_name]
    with tempfile.TemporaryDirectory(prefix="docseek-transaction-") as temp:
        base = Path(temp)
        root = base / "documents"
        db_path = base / "docseek.db"
        create_workload(root, files, workload)
        database = SearchDatabase(db_path)
        commit_latencies: list[float] = []
        transaction_samples: list[tuple[float, float]] = []
        gc.collect()
        rss = RssSampler()
        with rss:
            first = timed_scan(
                DirectoryIndexer(database, scan_backend=backend_factory()),
                root,
                profile_phases=True,
                transaction_capacity=capacity,
                commit_latencies=commit_latencies,
                transaction_samples=transaction_samples,
            )
        (
            first_seconds,
            scanner_wait,
            discovery_complete,
            candidates,
            stats,
            timings,
        ) = first
        snapshot = database_snapshot(db_path, root)
        expected = {
            "files": files,
            "integrity": "ok",
        }
        if stats.indexed != files or snapshot["files"] != files:
            raise RuntimeError(
                f"{workload.name}/{backend_name}/{capacity_label} indexed={stats.indexed} "
                f"db_files={snapshot['files']} expected={files}"
            )
        if snapshot["integrity"] != "ok":
            raise RuntimeError(
                f"{workload.name}/{backend_name}/{capacity_label} integrity={snapshot['integrity']!r}"
            )

        SearchDatabase(db_path)
        reopened_snapshot = database_snapshot(db_path, root)
        assert_database_parity(
            snapshot,
            reopened_snapshot,
            label=f"{workload.name}/{backend_name}/{capacity_label}",
        )

        cancel_probe = run_cancel_probe(
            files=cancel_files,
            workload=WORKLOADS["tiny-text"],
            backend_name=backend_name,
            transaction_capacity=capacity,
        )
        commit_total = timings.get("sql_commit", 0.0)
        transaction_count = len(transaction_samples)
        transaction_rows = [sample[0] for sample in transaction_samples]
        transaction_chunks = [sample[1] for sample in transaction_samples]
        return {
            "workload": workload.name,
            "files_requested": files,
            "backend": backend_name,
            "capacity_label": capacity_label,
            "transaction_capacity": capacity,
            "first_scan_seconds": first_seconds,
            "scanner_wait_seconds": scanner_wait,
            "discovery_complete_seconds": discovery_complete,
            "candidates": candidates,
            "indexed": stats.indexed,
            "chunks": stats.chunks,
            "writer_total_seconds": (
                timings.get("writer_replace", 0.0)
                + timings.get("writer_flush_outer", 0.0)
                + timings.get("writer_exit", 0.0)
            ),
            "sql_total_seconds": sum(
                timings.get(name, 0.0)
                for name in (
                    "sql_begin",
                    "sql_savepoint",
                    "sql_files",
                    "sql_file_id",
                    "sql_delete_lookup",
                    "sql_delete_chunks",
                    "sql_chunks",
                    "sql_structure",
                    "sql_fts_cjk",
                    "sql_fts_normal",
                    "sql_extraction_state",
                    "sql_commit",
                    "sql_wal_checkpoint",
                )
            ),
            "commit_total_seconds": commit_total,
            "transaction_count": transaction_count,
            "commit_count": int(timings.get("writer_commit_count", 0.0)),
            "avg_commit_seconds": statistics.mean(commit_latencies)
            if commit_latencies
            else 0.0,
            "commit_p50_seconds": percentile(commit_latencies, 0.50),
            "commit_p95_seconds": percentile(commit_latencies, 0.95),
            "commit_max_seconds": max(commit_latencies, default=0.0),
            "commit_latencies_seconds": commit_latencies,
            "rows_per_transaction": statistics.mean(transaction_rows)
            if transaction_rows
            else 0.0,
            "chunks_per_transaction": statistics.mean(transaction_chunks)
            if transaction_chunks
            else 0.0,
            "max_transaction_rows": max(transaction_rows, default=0.0),
            "max_transaction_chunks": max(transaction_chunks, default=0.0),
            "fts_normal_seconds": timings.get("sql_fts_normal", 0.0),
            "fts_cjk_seconds": timings.get("sql_fts_cjk", 0.0),
            "cjk_tokenize_seconds": timings.get("cjk_tokens", 0.0),
            "raw_encode_seconds": timings.get("raw_encode", 0.0),
            "rss_start_mb": rss.start_mb,
            "rss_peak_mb": rss.peak_mb,
            "rss_delta_mb": rss.delta_mb,
            "validation": snapshot,
            "reopen_validation": reopened_snapshot,
            "cancel": cancel_probe,
            "expected": expected,
        }


def print_case(result: dict[str, Any]) -> None:
    print(
        f"case={result['workload']} backend={result['backend']} "
        f"capacity={result['capacity_label']}({result['transaction_capacity']}) "
        f"first_scan={result['first_scan_seconds']:.3f}s "
        f"writer_total={result['writer_total_seconds']:.3f}s "
        f"sql_total={result['sql_total_seconds']:.3f}s "
        f"commit_total={result['commit_total_seconds']:.3f}s "
        f"transactions={result['transaction_count']} commits={result['commit_count']} "
        f"rows_per_tx={result['rows_per_transaction']:.1f} "
        f"max_rows_per_tx={result['max_transaction_rows']:.0f} "
        f"commit_avg={result['avg_commit_seconds']:.3f}s "
        f"commit_p50={result['commit_p50_seconds']:.3f}s "
        f"commit_p95={result['commit_p95_seconds']:.3f}s "
        f"commit_max={result['commit_max_seconds']:.3f}s "
        f"fts_normal={result['fts_normal_seconds']:.3f}s "
        f"fts_cjk={result['fts_cjk_seconds']:.3f}s "
        f"cjk={result['cjk_tokenize_seconds']:.3f}s "
        f"rss_peak={result['rss_peak_mb']:.1f}MiB "
        f"rss_delta={result['rss_delta_mb']:.1f}MiB "
        f"cancel={result['cancel']['latency_ms']:.1f}ms "
        f"indexed={result['indexed']} chunks={result['chunks']} integrity={result['validation']['integrity']}"
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Benchmark-only SQLite transaction/commit capacity sweep"
    )
    parser.add_argument("--files", type=int, default=None)
    parser.add_argument("--workload", choices=tuple(WORKLOADS), action="append")
    parser.add_argument("--backend", choices=("python", "rust", "both"), default="both")
    parser.add_argument(
        "--capacities",
        nargs="+",
        default=list(DEFAULT_CAPACITY_TOKENS),
        help="transaction capacities; use 'files' for the upper-bound case",
    )
    parser.add_argument("--cancel-files", type=int, default=DEFAULT_CANCEL_FILES)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.cancel_files < 1:
        parser.error("--cancel-files must be >= 1")

    workload_names = args.workload or ["tiny-text", "medium-text", "large-text"]
    default_files = {
        "tiny-text": 50_000,
        "medium-text": 10_000,
        "large-text": 1_000,
    }
    backend_names = ("python", "rust") if args.backend == "both" else (args.backend,)
    results: list[dict[str, Any]] = []
    parity_oracles: dict[str, dict[str, Any]] = {}

    print("DocSeek SQLite transaction / commit A/B sweep")
    print(
        "benchmark-only override; production batch size, PRAGMA, journal mode, "
        "synchronous and tokenizer are unchanged"
    )
    print(f"capacities={args.capacities} backends={backend_names}")

    for workload_name in workload_names:
        workload = WORKLOADS[workload_name]
        files = args.files if args.files is not None else default_files.get(workload_name, 100)
        if files < 1:
            parser.error("--files must be >= 1")
        for capacity_token in args.capacities:
            capacity_label, capacity = parse_capacity(capacity_token, files)
            for backend_name in backend_names:
                result = run_case(
                    files=files,
                    workload=workload,
                    backend_name=backend_name,
                    capacity_label=capacity_label,
                    capacity=capacity,
                    cancel_files=args.cancel_files,
                )
                validation = result["validation"]
                oracle = parity_oracles.setdefault(workload_name, validation)
                if validation != oracle:
                    raise RuntimeError(
                        f"{workload_name}/{backend_name}/{capacity_label} diverged "
                        "from the first transaction-capacity parity oracle"
                    )
                results.append(result)
                print_case(result)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                {
                    "workloads": workload_names,
                    "backends": backend_names,
                    "capacities": args.capacities,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"json_report={args.json_out}")


if __name__ == "__main__":
    main()
