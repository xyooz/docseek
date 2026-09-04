from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .extractors import SUPPORTED_EXTENSIONS


def _is_under(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in roots:
        if resolved == root:
            return True
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


class _DocSeekEventHandler(FileSystemEventHandler):
    def __init__(self, notify: Callable[[], None], excluded_paths: list[Path]) -> None:
        super().__init__()
        self.notify = notify
        self.excluded_paths = excluded_paths

    def on_any_event(self, event: FileSystemEvent) -> None:
        src = Path(event.src_path)
        dest_path = getattr(event, "dest_path", None)
        dest = Path(dest_path) if dest_path else None

        if _is_under(src, self.excluded_paths) or (
            dest is not None and _is_under(dest, self.excluded_paths)
        ):
            return

        if event.is_directory:
            self.notify()
            return
        if src.suffix.lower() in SUPPORTED_EXTENSIONS or (
            dest is not None and dest.suffix.lower() in SUPPORTED_EXTENSIONS
        ):
            self.notify()


class WatchManager:
    """Watch indexed roots and collapse noisy filesystem events."""

    def __init__(self, on_change: Callable[[], None], *, debounce_seconds: float = 1.2) -> None:
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self._observer: Observer | None = None
        self._lock = threading.Lock()
        self._generation = 0

    def start(self, roots: list[str], excluded_paths: list[str] | None = None) -> None:
        self.stop()
        excluded = [Path(path).resolve() for path in (excluded_paths or [])]
        observer = Observer()
        handler = _DocSeekEventHandler(self._queue_change, excluded)
        scheduled = 0
        for root in roots:
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

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)

    def _queue_change(self) -> None:
        with self._lock:
            self._generation += 1
            generation = self._generation
        thread = threading.Thread(
            target=self._wait_and_notify,
            args=(generation,),
            daemon=True,
        )
        thread.start()

    def _wait_and_notify(self, generation: int) -> None:
        time.sleep(self.debounce_seconds)
        with self._lock:
            if generation != self._generation:
                return
        self.on_change()
