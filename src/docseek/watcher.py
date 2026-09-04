from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .extractors import SUPPORTED_EXTENSIONS


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _is_under(path: Path, roots: list[Path]) -> bool:
    resolved = _safe_resolve(path)
    for root in roots:
        if resolved == root:
            return True
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_supported(path: Path) -> bool:
    name = path.name
    return (
        not name.startswith("~$")
        and not name.endswith(".tmp")
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


@dataclass(frozen=True, slots=True)
class WatchBatch:
    paths: tuple[str, ...] = ()
    full_rescan: bool = False


class _DocSeekEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        queue_path: Callable[[Path], None],
        queue_rescan: Callable[[], None],
        excluded_paths: list[Path],
    ) -> None:
        super().__init__()
        self.queue_path = queue_path
        self.queue_rescan = queue_rescan
        self.excluded_paths = excluded_paths

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return

        src = Path(event.src_path)
        dest_path = getattr(event, "dest_path", None)
        dest = Path(dest_path) if dest_path else None

        if event.is_directory:
            src_excluded = _is_under(src, self.excluded_paths)
            dest_excluded = dest is None or _is_under(dest, self.excluded_paths)
            if not (src_excluded and dest_excluded):
                self.queue_rescan()
            return

        candidates = [src]
        if dest is not None:
            candidates.append(dest)

        for path in candidates:
            if _is_under(path, self.excluded_paths):
                continue
            if _is_supported(path):
                self.queue_path(path)


class WatchManager:
    """Watch indexed roots and collapse noisy events into one precise batch."""

    def __init__(
        self,
        on_change: Callable[[WatchBatch], None],
        *,
        debounce_seconds: float = 1.2,
    ) -> None:
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self._observer: Observer | None = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._pending_paths: set[str] = set()
        self._full_rescan = False
        self._config: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def start(self, roots: list[str], excluded_paths: list[str] | None = None) -> None:
        resolved_roots = tuple(sorted(str(_safe_resolve(Path(path))) for path in roots))
        resolved_excluded = tuple(
            sorted(str(_safe_resolve(Path(path))) for path in (excluded_paths or []))
        )
        config = (resolved_roots, resolved_excluded)

        if self._observer is not None and self._config == config:
            return

        self.stop()
        excluded = [Path(path) for path in resolved_excluded]
        observer = Observer()
        handler = _DocSeekEventHandler(self._queue_path, self._queue_rescan, excluded)
        scheduled = 0
        for root in resolved_roots:
            path = Path(root)
            if not path.exists() or not path.is_dir():
                continue
            try:
                observer.schedule(handler, str(path), recursive=True)
                scheduled += 1
            except OSError:
                continue
        if scheduled:
            observer.daemon = True
            observer.start()
            self._observer = observer
            self._config = config

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        self._config = None
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)

        with self._lock:
            timer = self._timer
            self._timer = None
            self._pending_paths.clear()
            self._full_rescan = False
        if timer is not None:
            timer.cancel()

    def _queue_path(self, path: Path) -> None:
        with self._lock:
            self._pending_paths.add(str(_safe_resolve(path)))
            self._restart_timer_locked()

    def _queue_rescan(self) -> None:
        with self._lock:
            self._full_rescan = True
            self._restart_timer_locked()

    def _restart_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        timer = threading.Timer(self.debounce_seconds, self._flush)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths = tuple(sorted(self._pending_paths))
            full_rescan = self._full_rescan
            self._pending_paths.clear()
            self._full_rescan = False
            self._timer = None

        if full_rescan or paths:
            self.on_change(WatchBatch(paths=paths, full_rescan=full_rescan))
