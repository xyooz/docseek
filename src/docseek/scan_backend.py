from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol

from .document_types import KNOWN_DOCUMENT_EXTENSIONS
from .file_exclusions import normalize_file_exclusion_patterns, matches_file_exclusion
from .index_priority import prioritize_index_candidates


MAX_SCAN_BATCH_SIZE = 128
# A batch may return before the candidate cap when traversal has done this much
# filesystem work.  Sparse trees therefore still publish progress and give
# callers a cancellation boundary even when almost every entry is filtered.
MAX_SCAN_WORK_ITEMS = 2_000
DEFAULT_MAX_FILE_SIZE = 200 * 1024 * 1024
DEFAULT_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
        "$recycle.bin",
        "system volume information",
    }
)


class ScanCancelled(RuntimeError):
    """Raised when a scan session is cancelled before its next batch."""


class RustScanBackendUnavailable(RuntimeError):
    """Raised when Rust cannot create a scan session before traversal starts."""


@dataclass(slots=True, frozen=True)
class ScanIssue:
    path: Path
    error_code: str
    detail: str


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


def _normalize_extension(value: object) -> str:
    extension = str(value).strip().lower()
    if not extension:
        return ""
    return extension if extension.startswith(".") else f".{extension}"


@dataclass(slots=True, frozen=True)
class ScanConfig:
    """Configuration shared by the Python and Rust scan backends."""

    enabled_extensions: Iterable[str] | None = None
    ignored_dir_names: Iterable[str] | None = None
    excluded_paths: Iterable[Path] | None = None
    excluded_file_patterns: Iterable[str] | None = None
    # Retained as shared index configuration. Discovery deliberately does not
    # enforce this limit: DirectoryIndexer must see oversized candidates so it
    # can remove stale rows and record the user-visible file_too_large issue.
    max_file_size: int | None = DEFAULT_MAX_FILE_SIZE

    def __post_init__(self) -> None:
        extensions = (
            KNOWN_DOCUMENT_EXTENSIONS
            if self.enabled_extensions is None
            else self.enabled_extensions
        )
        ignored_names = (
            DEFAULT_IGNORED_DIR_NAMES
            if self.ignored_dir_names is None
            else self.ignored_dir_names
        )
        excluded_paths = self.excluded_paths or ()
        patterns = self.excluded_file_patterns or ()
        object.__setattr__(
            self,
            "enabled_extensions",
            frozenset(
                extension
                for extension in (_normalize_extension(value) for value in extensions)
                if extension
            ),
        )
        object.__setattr__(
            self,
            "ignored_dir_names",
            frozenset(str(name).casefold() for name in ignored_names),
        )
        object.__setattr__(
            self,
            "excluded_paths",
            tuple(_safe_resolve(Path(path)) for path in excluded_paths),
        )
        object.__setattr__(
            self,
            "excluded_file_patterns",
            tuple(normalize_file_exclusion_patterns(patterns)),
        )
        if self.max_file_size is None:
            object.__setattr__(self, "max_file_size", DEFAULT_MAX_FILE_SIZE)


@dataclass(slots=True, frozen=True)
class ScanProgress:
    directories_seen: int = 0
    files_seen: int = 0
    candidates_discovered: int = 0
    candidates_emitted: int = 0
    excluded: int = 0
    errors: int = 0
    current_path: Path | None = None


@dataclass(slots=True)
class ScanBatch:
    candidates: list[Path]
    finished: bool
    progress: ScanProgress
    issues: list[ScanIssue] = field(default_factory=list)


class ScanSession(Protocol):
    def next_batch(self, max_items: int = MAX_SCAN_BATCH_SIZE) -> ScanBatch: ...

    def cancel(self) -> bool: ...

    def snapshot(self) -> ScanProgress | None: ...

    def is_finished(self) -> bool: ...


class ScanBackend(Protocol):
    def start_scan(self, root: Path, config: ScanConfig | None = None) -> ScanSession: ...


@dataclass(slots=True)
class _MutableProgress:
    directories_seen: int = 0
    files_seen: int = 0
    candidates_discovered: int = 0
    candidates_emitted: int = 0
    excluded: int = 0
    errors: int = 0
    current_path: Path | None = None

    def snapshot(self) -> ScanProgress:
        return ScanProgress(
            directories_seen=self.directories_seen,
            files_seen=self.files_seen,
            candidates_discovered=self.candidates_discovered,
            candidates_emitted=self.candidates_emitted,
            excluded=self.excluded,
            errors=self.errors,
            current_path=self.current_path,
        )


@dataclass(slots=True, frozen=True)
class _ScanProgressPulse:
    """Internal marker for a batch that only advances filesystem progress."""


def _scan_error_code(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "file_not_found"
    return "os_error"


class PythonScanSession:
    def __init__(self, root: Path, config: ScanConfig) -> None:
        self._root = _safe_resolve(Path(root))
        if not self._root.is_dir():
            raise NotADirectoryError(str(self._root))
        self._config = config
        self._cancel = threading.Event()
        self._progress = _MutableProgress(
            directories_seen=1,
            current_path=self._root,
        )
        self._finished = False
        self._exhausted = False
        self._pending_issues: list[ScanIssue] = []
        self._candidates = self._iter_candidates()

    def next_batch(self, max_items: int = MAX_SCAN_BATCH_SIZE) -> ScanBatch:
        if max_items <= 0:
            raise ValueError("scan batch size must be greater than zero")
        if self._finished:
            return ScanBatch([], True, self._progress.snapshot())

        batch_size = min(max_items, MAX_SCAN_BATCH_SIZE)
        candidates: list[Path] = []
        try:
            while len(candidates) < batch_size and not self._exhausted:
                self._checkpoint()
                try:
                    item = next(self._candidates)
                except StopIteration:
                    self._exhausted = True
                    break
                if isinstance(item, _ScanProgressPulse):
                    break
                candidates.append(item)
        except ScanCancelled:
            self._finished = True
            raise

        if self._exhausted and not candidates:
            self._finished = True
        self._progress.candidates_emitted += len(candidates)
        ordered = list(prioritize_index_candidates(candidates))
        issues = self._pending_issues
        self._pending_issues = []
        return ScanBatch(ordered, self._finished, self._progress.snapshot(), issues)

    def cancel(self) -> bool:
        if self._finished:
            return False
        self._cancel.set()
        return True

    def snapshot(self) -> ScanProgress:
        return self._progress.snapshot()

    def is_finished(self) -> bool:
        return self._finished

    def _checkpoint(self) -> None:
        if self._cancel.is_set():
            raise ScanCancelled("scan cancelled")

    def _record_issue(self, path: Path, exc: OSError) -> None:
        self._progress.errors += 1
        self._pending_issues.append(
            ScanIssue(path, _scan_error_code(exc), str(exc))
        )

    def _is_excluded(self, path: Path) -> bool:
        if not self._config.excluded_paths:
            return False
        normalized = _safe_resolve(path)
        return any(
            normalized == excluded or excluded in normalized.parents
            for excluded in self._config.excluded_paths
        )

    def _iter_candidates(self) -> Iterator[Path | _ScanProgressPulse]:
        pending_directories = [self._root]
        visited_directories = {self._root}
        work_items = 0
        while pending_directories:
            self._checkpoint()
            directory = pending_directories.pop()
            work_items += 1
            if self._is_excluded(directory):
                self._progress.excluded += 1
                if work_items >= MAX_SCAN_WORK_ITEMS:
                    work_items = 0
                    yield _ScanProgressPulse()
                continue
            self._progress.current_path = directory
            try:
                with os.scandir(directory) as entries:
                    while True:
                        self._checkpoint()
                        try:
                            entry = next(entries)
                        except StopIteration:
                            break
                        except OSError as exc:
                            self._record_issue(directory, exc)
                            work_items += 1
                            if work_items >= MAX_SCAN_WORK_ITEMS:
                                work_items = 0
                                yield _ScanProgressPulse()
                            break

                        work_items += 1
                        path = Path(entry.path)
                        self._progress.current_path = path
                        candidate: Path | None = None
                        try:
                            if self._is_excluded(path):
                                self._progress.excluded += 1
                            elif entry.is_dir():
                                name = entry.name.casefold()
                                if name not in self._config.ignored_dir_names:
                                    identity = (
                                        _safe_resolve(path)
                                        if entry.is_symlink()
                                        else path
                                    )
                                    if identity not in visited_directories:
                                        visited_directories.add(identity)
                                        pending_directories.append(path)
                                        self._progress.directories_seen += 1
                            elif entry.is_file():
                                self._progress.files_seen += 1
                                if (
                                    not entry.name.startswith("~$")
                                    and not entry.name.endswith(".tmp")
                                    and path.suffix.lower()
                                    in self._config.enabled_extensions
                                ):
                                    if matches_file_exclusion(
                                        path, self._config.excluded_file_patterns
                                    ):
                                        self._progress.excluded += 1
                                    else:
                                        self._progress.candidates_discovered += 1
                                        candidate = path
                        except OSError as exc:
                            self._record_issue(path, exc)
                        if candidate is not None:
                            yield candidate
                        if work_items >= MAX_SCAN_WORK_ITEMS:
                            work_items = 0
                            yield _ScanProgressPulse()
            except OSError as exc:
                self._record_issue(directory, exc)
                if work_items >= MAX_SCAN_WORK_ITEMS:
                    work_items = 0
                    yield _ScanProgressPulse()


class PythonScanBackend:
    scan_backend_name = "python"
    allow_fallback = False

    def start_scan(self, root: Path, config: ScanConfig | None = None) -> PythonScanSession:
        return PythonScanSession(Path(root), config or ScanConfig())


def _progress_from_rust(snapshot: object) -> ScanProgress:
    current_path = getattr(snapshot, "current_path", None)
    return ScanProgress(
        directories_seen=int(getattr(snapshot, "directories_seen", 0)),
        files_seen=int(getattr(snapshot, "files_seen", 0)),
        candidates_discovered=int(getattr(snapshot, "candidates_discovered", 0)),
        candidates_emitted=int(getattr(snapshot, "candidates_emitted", 0)),
        excluded=int(getattr(snapshot, "excluded", 0)),
        errors=int(getattr(snapshot, "errors", 0)),
        current_path=Path(current_path) if current_path else None,
    )


class RustScanSession:
    def __init__(self, session: object, controller: object) -> None:
        self._session = session
        # Keep cancellation on the controller object.  next_batch() holds a
        # mutable PyO3 borrow of the session while it releases the GIL for
        # discovery; calling session.cancel() from another Python thread can
        # therefore contend with that borrow.  The controller is a separate
        # PyO3 object backed by the same core cancellation token.
        self._controller = controller

    def next_batch(self, max_items: int = MAX_SCAN_BATCH_SIZE) -> ScanBatch:
        try:
            batch = self._session.next_batch(max_items)
        except InterruptedError as exc:
            raise ScanCancelled("scan cancelled") from exc
        return ScanBatch(
            candidates=[Path(path) for path in batch.candidates],
            finished=bool(batch.finished),
            progress=_progress_from_rust(batch),
            issues=[
                ScanIssue(
                    Path(issue.path),
                    str(issue.error_code),
                    str(issue.detail),
                )
                for issue in getattr(batch, "issues", ())
            ],
        )

    def cancel(self) -> bool:
        return bool(self._controller.cancel())

    def snapshot(self) -> ScanProgress | None:
        snapshot = self._session.snapshot()
        return _progress_from_rust(snapshot) if snapshot is not None else None

    def is_finished(self) -> bool:
        return bool(self._session.is_finished())


class RustScanBackend:
    scan_backend_name = "rust"

    def __init__(
        self,
        rust_module: object | None = None,
        *,
        allow_fallback: bool = False,
    ) -> None:
        self._rust_module = rust_module
        self.allow_fallback = allow_fallback

    def _module(self) -> object:
        if self._rust_module is not None:
            return self._rust_module
        try:
            import docseek_rust
        except ImportError as exc:
            raise RustScanBackendUnavailable(
                "docseek_rust is not installed; build the PyO3 wheel first"
            ) from exc
        self._rust_module = docseek_rust
        return docseek_rust

    def start_scan(self, root: Path, config: ScanConfig | None = None) -> RustScanSession:
        config = config or ScanConfig()
        try:
            controller = self._module().JobController()
            session = controller.start_scan(
                Path(root),
                sorted(config.enabled_extensions),
                sorted(config.ignored_dir_names),
                list(config.excluded_paths),
                list(config.excluded_file_patterns),
                # Keep file-size policy in DirectoryIndexer. If this value is
                # forwarded, discovery would swallow oversized files before
                # the indexer can remove stale rows and record file_too_large.
                None,
            )
            return RustScanSession(session, controller)
        except RustScanBackendUnavailable:
            raise
        except Exception as exc:
            # This boundary is deliberately limited to session construction.
            # Once RustScanSession has been returned, traversal errors must
            # propagate instead of restarting discovery through Python.
            raise RustScanBackendUnavailable(
                "Rust scan backend could not initialize a scan session"
            ) from exc


def resolve_scan_backend(name: str | None = None) -> ScanBackend:
    """Resolve the production backend without importing Rust eagerly.

    ``auto`` keeps Rust optional and permits a Python fallback only if Rust
    cannot create the session. ``rust`` is strict and lets that startup error
    reach the caller. Both modes defer importing the extension until scanning
    actually starts.
    """

    selected = (
        name if name is not None else os.environ.get("DOCSEEK_SCAN_BACKEND", "")
    )
    selected = selected.strip().casefold() or "auto"
    if selected == "python":
        return PythonScanBackend()
    if selected == "auto":
        return RustScanBackend(allow_fallback=True)
    if selected == "rust":
        return RustScanBackend()
    raise ValueError(
        "DOCSEEK_SCAN_BACKEND must be one of 'auto', 'python', or 'rust'"
    )


def iter_scan_candidates(
    session: ScanSession,
    *,
    max_items: int = MAX_SCAN_BATCH_SIZE,
    on_discovery: Callable[[Path, int], None] | None = None,
    on_progress: Callable[[ScanProgress], None] | None = None,
    on_issues: Callable[[list[ScanIssue]], None] | None = None,
) -> Iterator[Path]:
    """Replay session batches through the existing discovery callback shape."""

    initial = session.snapshot()
    if on_progress is not None and initial is not None:
        on_progress(initial)
    if (
        on_discovery is not None
        and initial is not None
        and initial.current_path is not None
    ):
        on_discovery(initial.current_path, initial.candidates_discovered)

    while True:
        batch = session.next_batch(max_items)
        if on_progress is not None:
            on_progress(batch.progress)
        if on_issues is not None and batch.issues:
            on_issues(batch.issues)
        if on_discovery is not None and batch.progress.current_path is not None:
            on_discovery(
                batch.progress.current_path,
                batch.progress.candidates_discovered,
            )
        if batch.finished:
            return
        yield from batch.candidates


__all__ = [
    "DEFAULT_IGNORED_DIR_NAMES",
    "DEFAULT_MAX_FILE_SIZE",
    "MAX_SCAN_BATCH_SIZE",
    "MAX_SCAN_WORK_ITEMS",
    "PythonScanBackend",
    "PythonScanSession",
    "RustScanBackend",
    "RustScanBackendUnavailable",
    "RustScanSession",
    "ScanBackend",
    "ScanBatch",
    "ScanCancelled",
    "ScanConfig",
    "ScanProgress",
    "ScanSession",
    "ScanIssue",
    "resolve_scan_backend",
    "iter_scan_candidates",
]
