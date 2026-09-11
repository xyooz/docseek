from __future__ import annotations

import argparse
import io
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import docseek.chunk_writer as chunk_writer_module
import docseek.indexer as indexer_module
import docseek.structure_store as structure_store_module
from docseek.indexer import DirectoryIndexer
from docseek.persistent_extraction import PersistentExtractionWorker
from docseek.scan_backend import PythonScanBackend, RustScanBackend
from docseek.search_db import SearchDatabase


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    name: str
    extension: str
    text_bytes: int | None = None
    fixture_name: str | None = None


WORKLOADS = {
    "tiny-text": WorkloadSpec("tiny-text", ".txt"),
    "medium-text": WorkloadSpec("medium-text", ".txt", text_bytes=16 * 1024),
    "large-text": WorkloadSpec("large-text", ".txt", text_bytes=512 * 1024),
    "docx": WorkloadSpec("docx", ".docx", fixture_name="testWORD.docx"),
    "xlsx": WorkloadSpec("xlsx", ".xlsx", fixture_name="testEXCEL.xlsx"),
    "pptx": WorkloadSpec("pptx", ".pptx", fixture_name="testPPT.pptx"),
    "pdf": WorkloadSpec("pdf", ".pdf", fixture_name="testPDF.pdf"),
}


def _official_fixture_path(fixture_name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "official" / fixture_name


def _write_text_workload_file(path: Path, index: int, target_bytes: int | None) -> None:
    if target_bytes is None:
        path.write_text(
            f"客户经理 信贷 业务制度 文件 {index}\n第二行办公资料",
            encoding="utf-8",
        )
        return

    line = (
        f"客户经理 信贷 业务制度 文件 {index}；"
        "用于 extraction profiling 的确定性文本行。\n"
    )
    line_bytes = len(line.encode("utf-8"))
    repetitions = max(1, math.ceil(target_bytes / line_bytes))
    path.write_text(line * repetitions, encoding="utf-8")


def create_workload(root: Path, count: int, workload: WorkloadSpec) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    first_path: Path | None = None
    source = (
        _official_fixture_path(workload.fixture_name)
        if workload.fixture_name is not None
        else None
    )
    if source is not None and not source.is_file():
        raise FileNotFoundError(f"official fixture not found: {source}")

    for index in range(count):
        destination = root / f"document_{index:06d}{workload.extension}"
        if source is None:
            _write_text_workload_file(destination, index, workload.text_bytes)
        else:
            shutil.copy2(source, destination)
        if first_path is None:
            first_path = destination

    if first_path is None:
        raise ValueError("workload must contain at least one file")
    return first_path


def create_files(root: Path, count: int) -> None:
    """Backward-compatible tiny-text workload helper."""
    create_workload(root, count, WORKLOADS["tiny-text"])


def _make_root_matcher(root: Path) -> Callable[[Path], bool]:
    """Match scanner paths even when the temporary directory uses a symlink."""
    raw_root = os.fspath(Path(root))
    canonical_root = os.fspath(Path(root).resolve())
    root_prefixes = tuple(
        prefix.rstrip(os.sep) + os.sep
        for prefix in {raw_root, canonical_root}
    )
    cache: dict[str, bool] = {}

    def matches(path: Path) -> bool:
        path = Path(path)
        key = os.fspath(path)
        cached = cache.get(key)
        if cached is not None:
            return cached
        result = any(
            key == prefix[:-1] or key.startswith(prefix)
            for prefix in root_prefixes
        )
        cache[key] = result
        return result

    return matches


class _TimedBufferedReader(io.BufferedReader):
    """Benchmark-only binary reader that measures actual Python file reads."""

    def __init__(self, raw: Any, record_read: Callable[[float], None]) -> None:
        super().__init__(raw)
        self._record_read = record_read

    def _timed(self, method: Callable[..., Any], *args: Any) -> Any:
        started = time.perf_counter()
        try:
            return method(*args)
        finally:
            self._record_read(time.perf_counter() - started)

    def read(self, size: int = -1) -> bytes:
        return self._timed(super().read, size)

    def read1(self, size: int = -1) -> bytes:
        return self._timed(super().read1, size)

    def readline(self, size: int = -1) -> bytes:
        return self._timed(super().readline, size)

    def readinto(self, buffer: Any) -> int:
        return self._timed(super().readinto, buffer)

    def readinto1(self, buffer: Any) -> int:
        return self._timed(super().readinto1, buffer)

    def readall(self) -> bytes:
        return self._timed(super().readall)


class _TimedTextLines:
    """Measure TextIOWrapper decode/newline work separately from chunk building."""

    def __init__(self, source: Iterator[str], record_decode: Callable[[float], None]) -> None:
        self._source = source
        self._record_decode = record_decode
        self.elapsed = 0.0

    def __iter__(self) -> "_TimedTextLines":
        return self

    def __next__(self) -> str:
        started = time.perf_counter()
        try:
            return next(self._source)
        finally:
            elapsed = time.perf_counter() - started
            self.elapsed += elapsed
            self._record_decode(elapsed)


def install_extraction_profiler(
    root: Path,
    add_timing: Callable[[str, float], None],
) -> list[Callable[[], None]]:
    """Install benchmark-only extraction boundary probes.

    The probes deliberately live here instead of production modules. Some
    timings are nested diagnostics: ``adapter_*`` and ``text_path_total``
    contain the finer-grained read/decode/chunk timings and must not be added
    to ``extraction_total``.
    """
    import docseek.chunks as chunks_module
    import docseek.document_adapters as adapters_module
    import docseek.extraction_broker as broker_module
    import docseek.legacy_isolation as isolation_module

    is_target_path = _make_root_matcher(root)
    restorers: list[Callable[[], None]] = []
    stage_totals: dict[str, float] = {}

    def record_stage(name: str, elapsed: float) -> None:
        stage_totals[name] = stage_totals.get(name, 0.0) + elapsed
        add_timing(name, elapsed)

    original_path_open = Path.open

    def profiled_path_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        handle = original_path_open(path, mode, buffering, encoding, errors, newline)
        if "b" not in mode or not is_target_path(path):
            return handle
        return _TimedBufferedReader(
            handle,
            lambda elapsed: record_stage("file_read", elapsed),
        )

    Path.open = profiled_path_open  # type: ignore[method-assign]
    restorers.append(lambda: setattr(Path, "open", original_path_open))

    original_decode = chunks_module._decode_text_bytes

    def timed_decode(data: bytes) -> tuple[str, str]:
        started = time.perf_counter()
        try:
            return original_decode(data)
        finally:
            record_stage("text_decode", time.perf_counter() - started)

    chunks_module._decode_text_bytes = timed_decode  # type: ignore[assignment]
    restorers.append(
        lambda: setattr(chunks_module, "_decode_text_bytes", original_decode)
    )

    original_iter_lines = chunks_module._iter_text_chunks_from_lines

    def timed_iter_lines(lines: Iterator[str], *args: Any, **kwargs: Any) -> Iterator[Any]:
        timed_lines: _TimedTextLines | Iterator[str]
        if isinstance(lines, io.TextIOBase):
            timed_lines = _TimedTextLines(
                lines,
                lambda elapsed: record_stage("text_decode_normalize", elapsed),
            )
        else:
            timed_lines = lines

        iterator = iter(original_iter_lines(timed_lines, *args, **kwargs))
        while True:
            started = time.perf_counter()
            line_elapsed_before = (
                timed_lines.elapsed if isinstance(timed_lines, _TimedTextLines) else 0.0
            )
            try:
                item = next(iterator)
            except StopIteration:
                elapsed = time.perf_counter() - started
                line_elapsed = (
                    timed_lines.elapsed - line_elapsed_before
                    if isinstance(timed_lines, _TimedTextLines)
                    else 0.0
                )
                record_stage("text_chunk_build", max(0.0, elapsed - line_elapsed))
                return
            except BaseException:
                elapsed = time.perf_counter() - started
                line_elapsed = (
                    timed_lines.elapsed - line_elapsed_before
                    if isinstance(timed_lines, _TimedTextLines)
                    else 0.0
                )
                record_stage("text_chunk_build", max(0.0, elapsed - line_elapsed))
                raise
            elapsed = time.perf_counter() - started
            line_elapsed = (
                timed_lines.elapsed - line_elapsed_before
                if isinstance(timed_lines, _TimedTextLines)
                else 0.0
            )
            record_stage("text_chunk_build", max(0.0, elapsed - line_elapsed))
            yield item

    chunks_module._iter_text_chunks_from_lines = timed_iter_lines  # type: ignore[assignment]
    restorers.append(
        lambda: setattr(
            chunks_module,
            "_iter_text_chunks_from_lines",
            original_iter_lines,
        )
    )

    original_iter_text = chunks_module._iter_text_chunks

    def timed_iter_text(path: Path, *args: Any, **kwargs: Any) -> Iterator[Any]:
        nested_names = (
            "file_read",
            "text_decode",
            "text_decode_normalize",
            "text_chunk_build",
        )
        iterator = iter(original_iter_text(path, *args, **kwargs))

        def record_text_step(started: float, before: dict[str, float]) -> None:
            total = time.perf_counter() - started
            record_stage("text_path_total", total)
            nested = sum(
                max(0.0, stage_totals.get(name, 0.0) - before[name])
                for name in nested_names
            )
            record_stage(
                "text_normalize_chunk_build",
                max(0.0, total - nested),
            )

        while True:
            started = time.perf_counter()
            before = {name: stage_totals.get(name, 0.0) for name in nested_names}
            try:
                item = next(iterator)
            except StopIteration:
                record_text_step(started, before)
                return
            except BaseException:
                record_text_step(started, before)
                raise
            record_text_step(started, before)
            yield item

    chunks_module._iter_text_chunks = timed_iter_text  # type: ignore[assignment]
    restorers.append(
        lambda: setattr(chunks_module, "_iter_text_chunks", original_iter_text)
    )

    original_registry_adapter_for = adapters_module.DocumentAdapterRegistry.adapter_for

    def timed_registry_adapter_for(registry: Any, path: Path) -> Any:
        started = time.perf_counter()
        try:
            return original_registry_adapter_for(registry, path)
        finally:
            elapsed = time.perf_counter() - started
            if is_target_path(path):
                record_stage("broker_dispatch", elapsed)

    adapters_module.DocumentAdapterRegistry.adapter_for = (  # type: ignore[method-assign]
        timed_registry_adapter_for
    )
    restorers.append(
        lambda: setattr(
            adapters_module.DocumentAdapterRegistry,
            "adapter_for",
            original_registry_adapter_for,
        )
    )

    original_isolated = isolation_module.iter_legacy_chunks_isolated

    def timed_isolated(source: Path, *args: Any, **kwargs: Any) -> Iterator[Any]:
        iterator = iter(original_isolated(source, *args, **kwargs))
        while True:
            started = time.perf_counter()
            try:
                item = next(iterator)
            except StopIteration:
                elapsed = time.perf_counter() - started
                if is_target_path(source):
                    record_stage("adapter_isolated", elapsed)
                return
            except BaseException:
                elapsed = time.perf_counter() - started
                if is_target_path(source):
                    record_stage("adapter_isolated", elapsed)
                raise
            elapsed = time.perf_counter() - started
            if is_target_path(source):
                record_stage("adapter_isolated", elapsed)
            yield item

    isolation_module.iter_legacy_chunks_isolated = timed_isolated  # type: ignore[assignment]
    restorers.append(
        lambda: setattr(
            isolation_module,
            "iter_legacy_chunks_isolated",
            original_isolated,
        )
    )

    registry = broker_module.DEFAULT_EXTRACTION_BROKER.registry
    seen_adapter_types: set[type[Any]] = set()
    for registered_adapter in getattr(registry, "_adapters", ()):
        adapter_type = type(registered_adapter)
        if adapter_type in seen_adapter_types:
            continue
        seen_adapter_types.add(adapter_type)
        original_adapter_iter = adapter_type.iter_chunks

        def make_timed_adapter_iter(
            original: Callable[..., Any],
            fallback_name: str,
        ) -> Callable[..., Any]:
            def timed_adapter_iter(
                adapter: Any,
                path: Path,
                *args: Any,
                **kwargs: Any,
            ) -> Iterator[Any]:
                iterator = iter(original(adapter, path, *args, **kwargs))
                while True:
                    started = time.perf_counter()
                    try:
                        item = next(iterator)
                    except StopIteration:
                        elapsed = time.perf_counter() - started
                        if is_target_path(path):
                            adapter_name = str(getattr(adapter, "name", fallback_name))
                            record_stage(f"adapter_{adapter_name}", elapsed)
                        return
                    except BaseException:
                        elapsed = time.perf_counter() - started
                        if is_target_path(path):
                            adapter_name = str(getattr(adapter, "name", fallback_name))
                            record_stage(f"adapter_{adapter_name}", elapsed)
                        raise
                    elapsed = time.perf_counter() - started
                    if is_target_path(path):
                        adapter_name = str(getattr(adapter, "name", fallback_name))
                        record_stage(f"adapter_{adapter_name}", elapsed)
                    yield item

            return timed_adapter_iter

        adapter_type.iter_chunks = make_timed_adapter_iter(  # type: ignore[method-assign]
            original_adapter_iter,
            adapter_type.__name__,
        )
        restorers.append(
            lambda adapter_type=adapter_type, original_adapter_iter=original_adapter_iter: setattr(
                adapter_type,
                "iter_chunks",
                original_adapter_iter,
            )
        )

    return restorers


class TimedScanSession:
    """Benchmark-only proxy that times scanner waits without changing production code."""

    def __init__(self, session: Any, record_wait: Callable[[float], None]) -> None:
        self._session = session
        self._record_wait = record_wait

    def next_batch(self, max_items: int = 128) -> Any:
        started = time.perf_counter()
        try:
            return self._session.next_batch(max_items)
        finally:
            self._record_wait(time.perf_counter() - started)

    def cancel(self) -> Any:
        return self._session.cancel()

    def snapshot(self) -> Any:
        return self._session.snapshot()

    def is_finished(self) -> bool:
        return self._session.is_finished()


def timed_scan(
    indexer: DirectoryIndexer,
    root: Path,
    *,
    profile_phases: bool = False,
    transaction_capacity: int | None = None,
    commit_latencies: list[float] | None = None,
    transaction_samples: list[tuple[float, float]] | None = None,
) -> tuple[float, float, float, int, object, dict[str, float]]:
    if transaction_capacity is not None and transaction_capacity < 1:
        raise ValueError("transaction_capacity must be greater than zero")
    started = time.perf_counter()
    discovery_complete_elapsed: float | None = None
    scanner_wait_seconds = 0.0
    candidate_count = 0
    phase = "discovery"
    timings: dict[str, float] = {}
    writer_scope_depth = 0
    transaction_states: dict[int, dict[str, float]] = {}
    max_transaction_rows = 0.0
    max_transaction_chunks = 0.0
    db_path = Path(indexer.chunk_store.db_path)
    wal_path = Path(f"{db_path}-wal")
    timings["wal_bytes_before"] = float(wal_path.stat().st_size) if wal_path.exists() else 0.0

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

    def record_scanner_wait(elapsed: float) -> None:
        nonlocal scanner_wait_seconds
        scanner_wait_seconds += elapsed

    original_iter_scan_candidates = indexer_module.iter_scan_candidates

    def timed_iter_scan_candidates(
        session: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Iterator[Any]:
        yield from original_iter_scan_candidates(
            TimedScanSession(session, record_scanner_wait),
            *args,
            **kwargs,
        )

    def classify_writer_sql(sql: str) -> str | None:
        normalized = " ".join(sql.split()).upper()
        if normalized == "BEGIN IMMEDIATE":
            return "sql_begin"
        if normalized.startswith(("SAVEPOINT ", "RELEASE ", "ROLLBACK TO ")):
            return "sql_savepoint"
        if normalized.startswith("INSERT INTO FILES"):
            return "sql_files_insert"
        if normalized.startswith("UPDATE FILES"):
            return "sql_files_update"
        if normalized.startswith("SELECT ID FROM FILES WHERE PATH"):
            return "sql_file_id"
        if normalized.startswith("SELECT C.ID, C.CONTENT, F.FILENAME"):
            return "sql_delete_lookup"
        if normalized.startswith("DELETE FROM CHUNKS"):
            return "sql_delete_chunks"
        if normalized.startswith("INSERT INTO CHUNKS"):
            return "sql_chunks"
        if normalized.startswith("INSERT INTO CHUNK_STRUCTURE"):
            return "sql_structure"
        if normalized.startswith("INSERT INTO CHUNK_INDEX_CJK2"):
            if "VALUES ('DELETE'" in normalized:
                return "sql_fts_cjk_delete"
            return "sql_fts_cjk_insert"
        if normalized.startswith("INSERT INTO CHUNK_INDEX"):
            if "VALUES ('DELETE'" in normalized:
                return "sql_fts_normal_delete"
            return "sql_fts_normal_insert"
        if normalized.startswith("INSERT INTO EXTRACTION_STATE"):
            return "sql_extraction_state"
        if normalized.startswith("PRAGMA WAL_CHECKPOINT"):
            return "sql_wal_checkpoint"
        return None

    def writer_sql_aliases(category: str) -> tuple[str, ...]:
        if category == "sql_files_insert" or category == "sql_files_update":
            return ("sql_files",)
        if category.startswith("sql_fts_normal_"):
            return ("sql_fts_normal",)
        if category.startswith("sql_fts_cjk_"):
            return ("sql_fts_cjk",)
        return ()

    def record_writer_sql(
        conn: sqlite3.Connection,
        category: str | None,
        elapsed: float,
        cursor: sqlite3.Cursor | None,
        *,
        count_execute: bool = True,
    ) -> None:
        nonlocal max_transaction_rows, max_transaction_chunks
        if writer_scope_depth <= 0:
            return

        if count_execute:
            add_timing("sql_execute", elapsed)
            add_timing("sql_execute_count", 1.0)
        if category is None:
            return

        add_timing(category, elapsed)
        add_timing(f"{category}_count", 1.0)
        for alias in writer_sql_aliases(category):
            add_timing(alias, elapsed)

        rowcount = 0.0
        if cursor is not None:
            try:
                if int(cursor.rowcount) >= 0:
                    rowcount = float(cursor.rowcount)
            except (AttributeError, TypeError, ValueError):
                rowcount = 0.0

        if rowcount > 0:
            add_timing("sql_rows_affected", rowcount)
            add_timing(f"{category}_rows", rowcount)
            state = transaction_states.get(id(conn))
            if state is not None:
                state["rows"] += rowcount
                if category == "sql_chunks":
                    state["chunks"] += rowcount

        if category == "sql_begin":
            transaction_states[id(conn)] = {"rows": 0.0, "chunks": 0.0}
            add_timing("writer_transaction_count", 1.0)

    def finish_writer_transaction(conn: sqlite3.Connection, *, committed: bool) -> None:
        nonlocal max_transaction_rows, max_transaction_chunks
        if writer_scope_depth <= 0:
            return
        state = transaction_states.pop(id(conn), None)
        if state is None:
            return
        if committed:
            add_timing("writer_transaction_rows_total", state["rows"])
            add_timing("writer_transaction_chunks_total", state["chunks"])
            max_transaction_rows = max(max_transaction_rows, state["rows"])
            max_transaction_chunks = max(max_transaction_chunks, state["chunks"])
            if transaction_samples is not None:
                transaction_samples.append((state["rows"], state["chunks"]))
        else:
            add_timing("writer_transaction_rolled_back", 1.0)

    restorers: list[Callable[[], None]] = []
    if transaction_capacity is not None:
        original_transaction_capacity = indexer_module.FULL_SCAN_BATCH_SIZE
        indexer_module.FULL_SCAN_BATCH_SIZE = int(transaction_capacity)
        restorers.append(
            lambda: setattr(
                indexer_module,
                "FULL_SCAN_BATCH_SIZE",
                original_transaction_capacity,
            )
        )
    indexer_module.iter_scan_candidates = timed_iter_scan_candidates
    restorers.append(
        lambda: setattr(
            indexer_module,
            "iter_scan_candidates",
            original_iter_scan_candidates,
        )
    )
    if profile_phases:
        original_persistent_worker = indexer_module.PersistentExtractionWorker

        class TimedPersistentExtractionWorker(PersistentExtractionWorker):
            """Benchmark-only timings for the production persistent worker."""

            def start(self) -> None:
                was_alive = self.is_alive
                call_started = time.perf_counter()
                try:
                    return super().start()
                finally:
                    if not was_alive:
                        add_timing(
                            "persistent_worker_startup",
                            time.perf_counter() - call_started,
                        )
                        add_timing("persistent_worker_start_count", 1.0)

            def extract(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
                call_started = time.perf_counter()
                try:
                    replay = super().extract(*args, **kwargs)
                finally:
                    add_timing(
                        "persistent_request_total",
                        time.perf_counter() - call_started,
                    )
                    add_timing("persistent_request_count", 1.0)

                def timed_replay() -> Iterator[Any]:
                    replay_started = time.perf_counter()
                    try:
                        yield from replay
                    finally:
                        add_timing(
                            "spool_replay",
                            time.perf_counter() - replay_started,
                        )

                return timed_replay()

            @staticmethod
            def _validate_chunk_file(output: Path, expected_count: int) -> None:
                call_started = time.perf_counter()
                try:
                    return PersistentExtractionWorker._validate_chunk_file(
                        output,
                        expected_count,
                    )
                finally:
                    add_timing(
                        "spool_validate",
                        time.perf_counter() - call_started,
                    )

        indexer_module.PersistentExtractionWorker = TimedPersistentExtractionWorker
        restorers.append(
            lambda: setattr(
                indexer_module,
                "PersistentExtractionWorker",
                original_persistent_worker,
            )
        )

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
        restorers.append(lambda: setattr(indexer, "_normalize", original_normalize))

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

        # First-index detail profile. ``iter_document_chunks`` is lazy, so time
        # each ``next`` call rather than only the cheap generator construction.
        # The resulting extraction timer excludes downstream SQLite/FTS work.
        original_iter_document_chunks = indexer_module.iter_document_chunks

        def timed_iter_document_chunks(*args: Any, **kwargs: Any) -> Iterator[Any]:
            setup_started = time.perf_counter()
            iterator = iter(original_iter_document_chunks(*args, **kwargs))
            add_timing("extract_chunks", time.perf_counter() - setup_started)
            while True:
                next_started = time.perf_counter()
                try:
                    item = next(iterator)
                except StopIteration:
                    add_timing("extract_chunks", time.perf_counter() - next_started)
                    return
                except BaseException:
                    add_timing("extract_chunks", time.perf_counter() - next_started)
                    raise
                add_timing("extract_chunks", time.perf_counter() - next_started)
                yield item

        indexer_module.iter_document_chunks = timed_iter_document_chunks
        restorers.append(
            lambda: setattr(
                indexer_module,
                "iter_document_chunks",
                original_iter_document_chunks,
            )
        )
        restorers.extend(install_extraction_profiler(root, add_timing))

        original_cjk_bigrams = indexer.chunk_store._cjk_bigrams
        indexer.chunk_store._cjk_bigrams = timed_call(  # type: ignore[method-assign]
            "cjk_tokens", original_cjk_bigrams
        )
        restorers.append(
            lambda: setattr(indexer.chunk_store, "_cjk_bigrams", original_cjk_bigrams)
        )

        original_encode = chunk_writer_module.encode_chunk_content
        chunk_writer_module.encode_chunk_content = timed_call("raw_encode", original_encode)
        restorers.append(
            lambda: setattr(chunk_writer_module, "encode_chunk_content", original_encode)
        )

        # ``writer_residual`` used to lump all SQLite work, source validation and
        # the structure sidecar into one opaque bucket. A benchmark-only
        # Connection subclass times the actual execute/commit calls while the
        # ChunkBatchWriter is active. Production connection behavior is not
        # changed, and the same WAL/runtime pragmas are reproduced here.
        original_connect = indexer.chunk_store.connect

        class ProfilingConnection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                call_started = time.perf_counter()
                cursor: sqlite3.Cursor | None = None
                try:
                    cursor = super().execute(sql, parameters)
                    return cursor
                finally:
                    if writer_scope_depth > 0:
                        category = classify_writer_sql(sql)
                        record_writer_sql(
                            self,
                            category,
                            time.perf_counter() - call_started,
                            cursor,
                        )

            def executemany(
                self,
                sql: str,
                seq_of_parameters: Any,
            ) -> sqlite3.Cursor:
                call_started = time.perf_counter()
                cursor: sqlite3.Cursor | None = None
                try:
                    cursor = super().executemany(sql, seq_of_parameters)
                    return cursor
                finally:
                    if writer_scope_depth > 0:
                        add_timing(
                            "sql_executemany",
                            time.perf_counter() - call_started,
                        )
                        add_timing("sql_executemany_count", 1.0)
                        category = classify_writer_sql(sql)
                        record_writer_sql(
                            self,
                            category,
                            time.perf_counter() - call_started,
                            cursor,
                            count_execute=False,
                        )

            def executescript(self, sql_script: str) -> sqlite3.Cursor:
                call_started = time.perf_counter()
                cursor: sqlite3.Cursor | None = None
                try:
                    cursor = super().executescript(sql_script)
                    return cursor
                finally:
                    if writer_scope_depth > 0:
                        add_timing(
                            "sql_executescript",
                            time.perf_counter() - call_started,
                        )
                        add_timing("sql_executescript_count", 1.0)

            def commit(self) -> None:
                call_started = time.perf_counter()
                succeeded = False
                try:
                    result = super().commit()
                    succeeded = True
                    return result
                finally:
                    if writer_scope_depth > 0:
                        elapsed = time.perf_counter() - call_started
                        add_timing("sql_commit", elapsed)
                        add_timing("writer_commit_count", 1.0)
                        if commit_latencies is not None:
                            commit_latencies.append(elapsed)
                        finish_writer_transaction(self, committed=succeeded)

            def rollback(self) -> None:
                try:
                    return super().rollback()
                finally:
                    if writer_scope_depth > 0:
                        add_timing("writer_rollback_count", 1.0)
                        finish_writer_transaction(self, committed=False)

            def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
                try:
                    return super().__exit__(exc_type, exc_value, traceback)
                finally:
                    self.close()

        def profiled_connect() -> sqlite3.Connection:
            conn = sqlite3.connect(
                indexer.chunk_store.db_path,
                timeout=10,
                factory=ProfilingConnection,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-32768")
            return conn

        indexer.chunk_store.connect = profiled_connect  # type: ignore[method-assign]
        restorers.append(lambda: setattr(indexer.chunk_store, "connect", original_connect))

        original_write_structure = structure_store_module.write_structure
        structure_store_module.write_structure = timed_call(  # type: ignore[assignment]
            "structure_total", original_write_structure
        )
        restorers.append(
            lambda: setattr(
                structure_store_module,
                "write_structure",
                original_write_structure,
            )
        )

        writer_cls = chunk_writer_module.ChunkBatchWriter
        original_replace_document = writer_cls.replace_document

        def timed_replace_document(writer: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal writer_scope_depth
            add_timing("writer_replace_count", 1.0)
            validate_source = kwargs.get("validate_source")
            if validate_source is not None:
                def timed_validate_source() -> Any:
                    call_started = time.perf_counter()
                    try:
                        return validate_source()
                    finally:
                        add_timing("source_validate", time.perf_counter() - call_started)

                kwargs["validate_source"] = timed_validate_source

            call_started = time.perf_counter()
            writer_scope_depth += 1
            try:
                return original_replace_document(writer, *args, **kwargs)
            finally:
                writer_scope_depth -= 1
                add_timing("writer_replace", time.perf_counter() - call_started)

        writer_cls.replace_document = timed_replace_document  # type: ignore[method-assign]
        restorers.append(
            lambda: setattr(writer_cls, "replace_document", original_replace_document)
        )

        original_flush = writer_cls.flush

        def timed_flush(writer: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal writer_scope_depth
            call_started = time.perf_counter()
            outermost = writer_scope_depth == 0
            add_timing("writer_flush_count", 1.0)
            if outermost:
                writer_scope_depth += 1
                add_timing("writer_flush_outer_count", 1.0)
            try:
                return original_flush(writer, *args, **kwargs)
            finally:
                elapsed = time.perf_counter() - call_started
                if outermost:
                    writer_scope_depth -= 1
                    add_timing("writer_flush_outer", elapsed)
                add_timing("writer_flush", elapsed)

        writer_cls.flush = timed_flush  # type: ignore[method-assign]
        restorers.append(lambda: setattr(writer_cls, "flush", original_flush))

        original_exit = writer_cls.__exit__

        def timed_exit(writer: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal writer_scope_depth
            call_started = time.perf_counter()
            outermost = writer_scope_depth == 0
            add_timing("writer_exit_count", 1.0)
            if outermost:
                writer_scope_depth += 1
            try:
                return original_exit(writer, *args, **kwargs)
            finally:
                if outermost:
                    writer_scope_depth -= 1
                add_timing("writer_exit", time.perf_counter() - call_started)

        writer_cls.__exit__ = timed_exit  # type: ignore[method-assign]
        restorers.append(lambda: setattr(writer_cls, "__exit__", original_exit))

    def candidates_ready(count: int) -> None:
        nonlocal discovery_complete_elapsed, candidate_count, phase
        if discovery_complete_elapsed is None:
            discovery_complete_elapsed = time.perf_counter() - started
        candidate_count = count
        phase = "post"

    try:
        stats = indexer.scan(root, on_candidates_ready=candidates_ready)
    finally:
        for restore in reversed(restorers):
            restore()

    shm_path = Path(f"{db_path}-shm")
    timings["writer_transaction_max_rows"] = max_transaction_rows
    timings["writer_transaction_max_chunks"] = max_transaction_chunks
    timings["wal_bytes_after"] = float(wal_path.stat().st_size) if wal_path.exists() else 0.0
    timings["shm_bytes_after"] = float(shm_path.stat().st_size) if shm_path.exists() else 0.0

    total_seconds = time.perf_counter() - started
    return (
        total_seconds,
        scanner_wait_seconds,
        discovery_complete_elapsed or 0.0,
        candidate_count,
        stats,
        timings,
    )


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


def format_first_index_breakdown(timings: dict[str, float]) -> str:
    writer_replace = timings.get("writer_replace", 0.0)
    extraction = timings.get("extract_chunks", 0.0)
    cjk = timings.get("cjk_tokens", 0.0)
    raw_encode = timings.get("raw_encode", 0.0)
    flush = timings.get("writer_flush", 0.0)
    flush_outer = timings.get("writer_flush_outer", 0.0)
    writer_exit = timings.get("writer_exit", 0.0)
    candidate = timings.get("candidate_check", 0.0)
    writer_residual = max(0.0, writer_replace - extraction - cjk - raw_encode - flush)
    candidate_overhead = max(0.0, candidate - writer_replace)
    writer_total = writer_replace + flush_outer + writer_exit
    return (
        f"extraction_total={extraction:.3f}s cjk={cjk:.3f}s raw_encode={raw_encode:.3f}s "
        f"writer_total={writer_total:.3f}s writer_replace={writer_replace:.3f}s "
        f"writer_flush={flush:.3f}s writer_flush_outer={flush_outer:.3f}s "
        f"writer_exit={writer_exit:.3f}s "
        f"writer_residual={writer_residual:.3f}s candidate_overhead={candidate_overhead:.3f}s"
    )


def format_extraction_detail(timings: dict[str, float]) -> str:
    names = (
        "broker_dispatch",
        "file_read",
        "text_decode",
        "text_decode_normalize",
        "text_chunk_build",
        "text_normalize_chunk_build",
        "adapter_direct",
        "adapter_calamine-xlsx-fast",
        "adapter_calamine",
        "adapter_tika-native",
        "adapter_wps-local",
        "adapter_isolated",
    )
    details = " ".join(
        f"{name}={timings.get(name, 0.0):.3f}s"
        for name in names
        if name in timings or name in {"broker_dispatch", "adapter_isolated"}
    )
    return f"extraction_total={timings.get('extract_chunks', 0.0):.3f}s {details}"


def format_persistent_worker_detail(timings: dict[str, float]) -> str:
    return (
        f"persistent_worker_startup={timings.get('persistent_worker_startup', 0.0):.3f}s "
        f"persistent_worker_starts={timings.get('persistent_worker_start_count', 0.0):.0f} "
        f"persistent_request_total={timings.get('persistent_request_total', 0.0):.3f}s "
        f"persistent_requests={timings.get('persistent_request_count', 0.0):.0f} "
        f"spool_validate={timings.get('spool_validate', 0.0):.3f}s "
        f"spool_replay={timings.get('spool_replay', 0.0):.3f}s"
    )


def format_writer_detail(timings: dict[str, float]) -> str:
    structure_total = timings.get("structure_total", 0.0)
    structure_sql = timings.get("sql_structure", 0.0)
    structure_cpu = max(0.0, structure_total - structure_sql)
    sql_names = (
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
    sql_total = sum(timings.get(name, 0.0) for name in sql_names)
    transaction_count = timings.get("writer_transaction_count", 0.0)
    rows_total = timings.get("writer_transaction_rows_total", 0.0)
    chunks_total = timings.get("writer_transaction_chunks_total", 0.0)
    average_rows = rows_total / transaction_count if transaction_count else 0.0
    average_chunks = chunks_total / transaction_count if transaction_count else 0.0
    return (
        f"sql_total={sql_total:.3f}s "
        f"files_sql={timings.get('sql_files', 0.0):.3f}s "
        f"chunks_sql={timings.get('sql_chunks', 0.0):.3f}s "
        f"structure_total={structure_total:.3f}s "
        f"structure_sql={structure_sql:.3f}s structure_cpu={structure_cpu:.3f}s "
        f"fts_normal={timings.get('sql_fts_normal', 0.0):.3f}s "
        f"fts_cjk={timings.get('sql_fts_cjk', 0.0):.3f}s "
        f"state_sql={timings.get('sql_extraction_state', 0.0):.3f}s "
        f"savepoint_sql={timings.get('sql_savepoint', 0.0):.3f}s "
        f"begin_sql={timings.get('sql_begin', 0.0):.3f}s "
        f"commit_sql={timings.get('sql_commit', 0.0):.3f}s "
        f"source_validate={timings.get('source_validate', 0.0):.3f}s "
        f"file_id_sql={timings.get('sql_file_id', 0.0):.3f}s "
        f"delete_lookup={timings.get('sql_delete_lookup', 0.0):.3f}s "
        f"delete_chunks={timings.get('sql_delete_chunks', 0.0):.3f}s "
        f"files_insert={timings.get('sql_files_insert', 0.0):.3f}s "
        f"files_update={timings.get('sql_files_update', 0.0):.3f}s "
        f"files_insert_rows={timings.get('sql_files_insert_rows', 0.0):.0f} "
        f"files_update_rows={timings.get('sql_files_update_rows', 0.0):.0f} "
        f"chunk_delete_rows={timings.get('sql_delete_chunks_rows', 0.0):.0f} "
        f"state_rows={timings.get('sql_extraction_state_rows', 0.0):.0f} "
        f"fts_normal_insert={timings.get('sql_fts_normal_insert', 0.0):.3f}s "
        f"fts_normal_delete={timings.get('sql_fts_normal_delete', 0.0):.3f}s "
        f"fts_cjk_insert={timings.get('sql_fts_cjk_insert', 0.0):.3f}s "
        f"fts_cjk_delete={timings.get('sql_fts_cjk_delete', 0.0):.3f}s "
        f"execute={timings.get('sql_execute', 0.0):.3f}s "
        f"execute_calls={timings.get('sql_execute_count', 0.0):.0f} "
        f"executemany={timings.get('sql_executemany', 0.0):.3f}s "
        f"executemany_calls={timings.get('sql_executemany_count', 0.0):.0f} "
        f"executescript={timings.get('sql_executescript', 0.0):.3f}s "
        f"transactions={transaction_count:.0f} "
        f"commits={timings.get('writer_commit_count', 0.0):.0f} "
        f"rollbacks={timings.get('writer_rollback_count', 0.0):.0f} "
        f"rows_affected={timings.get('sql_rows_affected', 0.0):.0f} "
        f"rows_per_transaction={average_rows:.1f} "
        f"max_rows_per_transaction={timings.get('writer_transaction_max_rows', 0.0):.0f} "
        f"chunks_written={timings.get('sql_chunks_rows', 0.0):.0f} "
        f"chunks_per_transaction={average_chunks:.1f} "
        f"structure_rows={timings.get('sql_structure_rows', 0.0):.0f} "
        f"fts_normal_insert_rows={timings.get('sql_fts_normal_insert_rows', 0.0):.0f} "
        f"fts_normal_delete_rows={timings.get('sql_fts_normal_delete_rows', 0.0):.0f} "
        f"fts_cjk_insert_rows={timings.get('sql_fts_cjk_insert_rows', 0.0):.0f} "
        f"fts_cjk_delete_rows={timings.get('sql_fts_cjk_delete_rows', 0.0):.0f} "
        f"flush_calls={timings.get('writer_flush_count', 0.0):.0f} "
        f"outer_flush_calls={timings.get('writer_flush_outer_count', 0.0):.0f} "
        f"replace_calls={timings.get('writer_replace_count', 0.0):.0f} "
        f"wal_checkpoint={timings.get('sql_wal_checkpoint', 0.0):.3f}s "
        f"wal_checkpoint_calls={timings.get('sql_wal_checkpoint_count', 0.0):.0f} "
        f"wal_before={timings.get('wal_bytes_before', 0.0):.0f}B "
        f"wal_after={timings.get('wal_bytes_after', 0.0):.0f}B "
        f"shm_after={timings.get('shm_bytes_after', 0.0):.0f}B"
    )


def run_backend_benchmark(
    file_count: int,
    backend_name: str,
    workload: WorkloadSpec,
    *,
    require_persistent_worker: bool = False,
    profile_replacement: bool = False,
) -> None:
    backend_factory = {
        "python": PythonScanBackend,
        "rust": lambda: RustScanBackend(allow_fallback=False),
    }.get(backend_name)
    if backend_factory is None:
        raise ValueError(f"unsupported scan backend: {backend_name}")

    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        root = base / "documents"
        db = SearchDatabase(base / "docseek.db")
        changed = create_workload(root, file_count, workload)

        (
            first_seconds,
            first_scanner_wait,
            first_discovery_complete,
            first_candidates,
            first_stats,
            first_timings,
        ) = timed_scan(
            DirectoryIndexer(db, scan_backend=backend_factory()),
            root,
            profile_phases=True,
        )
        (
            second_seconds,
            second_scanner_wait,
            second_discovery_complete,
            second_candidates,
            second_stats,
            second_timings,
        ) = timed_scan(
            DirectoryIndexer(db, scan_backend=backend_factory()),
            root,
            profile_phases=True,
        )

        changed_stat = changed.stat()
        os.utime(
            changed,
            ns=(
                changed_stat.st_atime_ns,
                max(time.time_ns(), changed_stat.st_mtime_ns + 2_000_000_000),
            ),
        )

        replacement_result: tuple[
            float,
            float,
            float,
            int,
            object,
            dict[str, float],
        ] | None = None
        if profile_replacement:
            replacement_result = timed_scan(
                DirectoryIndexer(db, scan_backend=backend_factory()),
                root,
                profile_phases=True,
            )

        init_started = time.perf_counter()
        incremental_indexer = DirectoryIndexer(db, scan_backend=backend_factory())
        init_ms = (time.perf_counter() - init_started) * 1000

        update_started = time.perf_counter()
        update_stats = incremental_indexer.update_paths([changed])
        update_only_ms = (time.perf_counter() - update_started) * 1000
        total_update_ms = init_ms + update_only_ms

        first_tail_after_discovery = max(0.0, first_seconds - first_discovery_complete)
        second_tail_after_discovery = max(0.0, second_seconds - second_discovery_complete)

        if require_persistent_worker and workload.fixture_name is not None:
            persistent_requests = first_timings.get("persistent_request_count", 0.0)
            if persistent_requests < file_count:
                raise RuntimeError(
                    f"{workload.name}/{backend_name} did not use the persistent "
                    f"worker for every file: requests={persistent_requests:.0f}, "
                    f"expected_at_least={file_count}"
                )

        print("DocSeek directory scan benchmark")
        print(f"backend={backend_name}")
        print(f"workload={workload.name}")
        print(f"files={file_count:,}")
        if workload.fixture_name is not None:
            print(f"fixture={workload.fixture_name}")
        elif workload.text_bytes is not None:
            print(f"target_text_bytes_per_file={workload.text_bytes:,}")
        print(
            f"first_scan={first_seconds:.3f}s "
            f"scanner_wait={first_scanner_wait:.3f}s "
            f"discovery_complete_wall={first_discovery_complete:.3f}s "
            f"tail_after_discovery={first_tail_after_discovery:.3f}s "
            f"candidates={first_candidates:,} "
            f"scanner_wait_share={first_scanner_wait / first_seconds:.1%} "
            f"files_per_second={file_count / first_seconds:.1f} "
            f"indexed={first_stats.indexed}"
        )
        if first_timings:
            print("first_phases " + format_phase_timings(first_timings))
            print("first_extraction " + format_extraction_detail(first_timings))
            print("first_persistent " + format_persistent_worker_detail(first_timings))
            print("first_writer_breakdown " + format_first_index_breakdown(first_timings))
            print("first_writer_detail " + format_writer_detail(first_timings))
        print(
            f"unchanged_scan={second_seconds:.3f}s "
            f"scanner_wait={second_scanner_wait:.3f}s "
            f"discovery_complete_wall={second_discovery_complete:.3f}s "
            f"tail_after_discovery={second_tail_after_discovery:.3f}s "
            f"candidates={second_candidates:,} "
            f"scanner_wait_share={second_scanner_wait / second_seconds:.1%} "
            f"files_per_second={file_count / second_seconds:.1f} "
            f"unchanged={second_stats.unchanged}"
        )
        if second_timings:
            measured = sum(
                value
                for name, value in second_timings.items()
                if name not in {
                    "normalize_discovery",
                    "writer_replace",
                    "extract_chunks",
                    "cjk_tokens",
                    "raw_encode",
                    "writer_flush",
                    "writer_flush_outer",
                    "writer_exit",
                    "structure_total",
                    "source_validate",
                }
                and not name.startswith("sql_")
            )
            residual = max(0.0, second_tail_after_discovery - measured)
            normalize_post = second_timings.get("normalize_post", 0.0)
            missing_cleanup = second_timings.get("missing_cleanup", 0.0)
            hot_share = (
                (normalize_post + missing_cleanup) / second_tail_after_discovery
                if second_tail_after_discovery > 0
                else 0.0
            )
            normalize_per_1k_ms = (
                normalize_post * 1_000_000 / second_candidates
                if second_candidates > 0
                else 0.0
            )
            print(
                "unchanged_phases "
                + format_phase_timings(second_timings)
                + f" residual_post={residual:.3f}s"
                + f" path_hot_share={hot_share:.1%}"
                + f" normalize_per_1k={normalize_per_1k_ms:.2f}ms"
            )
        if replacement_result is not None:
            (
                replacement_seconds,
                replacement_scanner_wait,
                replacement_discovery_complete,
                replacement_candidates,
                replacement_stats,
                replacement_timings,
            ) = replacement_result
            replacement_tail = max(
                0.0,
                replacement_seconds - replacement_discovery_complete,
            )
            print(
                f"replacement_scan={replacement_seconds:.3f}s "
                f"scanner_wait={replacement_scanner_wait:.3f}s "
                f"discovery_complete_wall={replacement_discovery_complete:.3f}s "
                f"tail_after_discovery={replacement_tail:.3f}s "
                f"candidates={replacement_candidates:,} "
                f"scanner_wait_share={replacement_scanner_wait / replacement_seconds:.1%} "
                f"indexed={replacement_stats.indexed} "
                f"unchanged={replacement_stats.unchanged}"
            )
            print(
                "replacement_extraction "
                + format_extraction_detail(replacement_timings)
            )
            print(
                "replacement_writer_breakdown "
                + format_first_index_breakdown(replacement_timings)
            )
            print("replacement_writer_detail " + format_writer_detail(replacement_timings))
        print(
            f"single_file_update=backend-independent workload_touch total={total_update_ms:.2f}ms "
            f"indexer_init={init_ms:.2f}ms update_only={update_only_ms:.2f}ms "
            f"indexed={update_stats.indexed} removed={update_stats.removed}"
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DocSeek directory scan benchmark")
    parser.add_argument(
        "--files",
        type=int,
        default=1000,
        help="number of files or fixture copies in the selected workload",
    )
    parser.add_argument(
        "--workload",
        choices=tuple(WORKLOADS),
        default="tiny-text",
        help="input workload (default: tiny-text)",
    )
    parser.add_argument(
        "--backend",
        choices=("python", "rust", "both"),
        default="python",
        help="scan backend to profile (default: python)",
    )
    parser.add_argument(
        "--require-persistent-worker",
        action="store_true",
        help="fail Office/PDF workloads if production falls back to one-shot isolation",
    )
    parser.add_argument(
        "--profile-replacement",
        action="store_true",
        help="profile one changed-file reconciliation after the unchanged rescan",
    )
    args = parser.parse_args()
    if args.files < 1:
        parser.error("--files must be >= 1")

    backends = ("python", "rust") if args.backend == "both" else (args.backend,)
    workload = WORKLOADS[args.workload]
    for backend_name in backends:
        run_backend_benchmark(
            args.files,
            backend_name,
            workload,
            require_persistent_worker=args.require_persistent_worker,
            profile_replacement=args.profile_replacement,
        )


if __name__ == "__main__":
    main()
