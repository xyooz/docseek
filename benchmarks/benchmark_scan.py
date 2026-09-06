from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import docseek.chunk_writer as chunk_writer_module
import docseek.indexer as indexer_module
import docseek.structure_store as structure_store_module
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
    writer_scope_depth = 0

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

    def classify_writer_sql(sql: str) -> str | None:
        normalized = " ".join(sql.split()).upper()
        if normalized == "BEGIN IMMEDIATE":
            return "sql_begin"
        if normalized.startswith(("SAVEPOINT ", "RELEASE ", "ROLLBACK TO ")):
            return "sql_savepoint"
        if normalized.startswith("INSERT INTO FILES") or normalized.startswith("UPDATE FILES"):
            return "sql_files"
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
            return "sql_fts_cjk"
        if normalized.startswith("INSERT INTO CHUNK_INDEX"):
            return "sql_fts_normal"
        if normalized.startswith("INSERT INTO EXTRACTION_STATE"):
            return "sql_extraction_state"
        return None

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
                try:
                    return super().execute(sql, parameters)
                finally:
                    if writer_scope_depth > 0:
                        category = classify_writer_sql(sql)
                        if category is not None:
                            add_timing(category, time.perf_counter() - call_started)

            def commit(self) -> None:
                call_started = time.perf_counter()
                try:
                    return super().commit()
                finally:
                    if writer_scope_depth > 0:
                        add_timing("sql_commit", time.perf_counter() - call_started)

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
            if outermost:
                writer_scope_depth += 1
            try:
                return original_flush(writer, *args, **kwargs)
            finally:
                if outermost:
                    writer_scope_depth -= 1
                add_timing("writer_flush", time.perf_counter() - call_started)

        writer_cls.flush = timed_flush  # type: ignore[method-assign]
        restorers.append(lambda: setattr(writer_cls, "flush", original_flush))

        original_exit = writer_cls.__exit__

        def timed_exit(writer: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal writer_scope_depth
            call_started = time.perf_counter()
            outermost = writer_scope_depth == 0
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


def format_first_index_breakdown(timings: dict[str, float]) -> str:
    writer_replace = timings.get("writer_replace", 0.0)
    extraction = timings.get("extract_chunks", 0.0)
    cjk = timings.get("cjk_tokens", 0.0)
    raw_encode = timings.get("raw_encode", 0.0)
    flush = timings.get("writer_flush", 0.0)
    writer_exit = timings.get("writer_exit", 0.0)
    candidate = timings.get("candidate_check", 0.0)
    writer_residual = max(0.0, writer_replace - extraction - cjk - raw_encode - flush)
    candidate_overhead = max(0.0, candidate - writer_replace)
    return (
        f"extract={extraction:.3f}s cjk={cjk:.3f}s raw_encode={raw_encode:.3f}s "
        f"writer_flush={flush:.3f}s writer_exit={writer_exit:.3f}s "
        f"writer_residual={writer_residual:.3f}s candidate_overhead={candidate_overhead:.3f}s"
    )


def format_writer_detail(timings: dict[str, float]) -> str:
    structure_total = timings.get("structure_total", 0.0)
    structure_sql = timings.get("sql_structure", 0.0)
    structure_cpu = max(0.0, structure_total - structure_sql)
    return (
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
        f"delete_chunks={timings.get('sql_delete_chunks', 0.0):.3f}s"
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
            first_timings,
        ) = timed_scan(DirectoryIndexer(db), root, profile_phases=True)
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
        if first_timings:
            print("first_phases " + format_phase_timings(first_timings))
            print("first_writer_breakdown " + format_first_index_breakdown(first_timings))
            print("first_writer_detail " + format_writer_detail(first_timings))
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
                if name not in {
                    "normalize_discovery",
                    "writer_replace",
                    "extract_chunks",
                    "cjk_tokens",
                    "raw_encode",
                    "writer_flush",
                    "writer_exit",
                    "structure_total",
                    "source_validate",
                }
                and not name.startswith("sql_")
            )
            residual = max(0.0, second_post_discovery - measured)
            normalize_post = second_timings.get("normalize_post", 0.0)
            missing_cleanup = second_timings.get("missing_cleanup", 0.0)
            hot_share = (
                (normalize_post + missing_cleanup) / second_post_discovery
                if second_post_discovery > 0
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
        print(
            f"single_file_total={total_update_ms:.2f}ms "
            f"indexer_init={init_ms:.2f}ms update_only={update_only_ms:.2f}ms "
            f"indexed={update_stats.indexed} removed={update_stats.removed}"
        )


if __name__ == "__main__":
    main()
