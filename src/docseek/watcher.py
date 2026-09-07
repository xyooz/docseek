from __future__ import annotations

import threading
import time
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
    """Watch indexed roots and converge them with periodic reconciliation.

    Native filesystem notifications provide low-latency updates, but they are
    not treated as the sole source of truth. A bounded periodic full rescan is
    the safety net for events missed during sleep/resume, observer disruption,
    or platform-specific notification loss.

    The normal debounce window collapses noisy Office/WPS save sequences. A
    maximum batch delay prevents continuous file activity from postponing the
    flush forever. A lightweight health timer also verifies the watchdog
    observer remains alive and detects long scheduling gaps characteristic of
    suspend/resume. Both cases trigger reconciliation rather than trusting that
    no filesystem notifications were missed.
    """

    def __init__(
        self,
        on_change: Callable[[WatchBatch], None],
        *,
        debounce_seconds: float = 1.2,
        max_batch_delay_seconds: float = 10.0,
        reconcile_seconds: float = 3600.0,
        health_check_seconds: float = 60.0,
        resume_gap_seconds: float | None = None,
    ) -> None:
        self.on_change = on_change
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.max_batch_delay_seconds = max(
            self.debounce_seconds,
            float(max_batch_delay_seconds),
        )
        self.reconcile_seconds = max(0.0, float(reconcile_seconds))
        self.health_check_seconds = max(0.0, float(health_check_seconds))
        default_resume_gap = max(120.0, self.health_check_seconds * 3.0)
        self.resume_gap_seconds = max(
            self.health_check_seconds,
            float(default_resume_gap if resume_gap_seconds is None else resume_gap_seconds),
        )
        self._observer: Observer | None = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._reconcile_timer: threading.Timer | None = None
        self._health_timer: threading.Timer | None = None
        self._last_health_check: float | None = None
        self._pending_since: float | None = None
        self._pending_paths: set[str] = set()
        self._full_rescan = False
        self._config: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def start(self, roots: list[str], excluded_paths: list[str] | None = None) -> None:
        resolved_roots = tuple(sorted(str(_safe_resolve(Path(path))) for path in roots))
        resolved_excluded = tuple(
            sorted(str(_safe_resolve(Path(path))) for path in (excluded_paths or []))
        )
        config = (resolved_roots, resolved_excluded)

        if (
            self._observer is not None
            and self._config == config
            and self._observer.is_alive()
        ):
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
            with self._lock:
                self._last_health_check = time.monotonic()
                self._schedule_reconcile_locked()
                self._schedule_health_check_locked()

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        self._config = None
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)

        with self._lock:
            timer = self._timer
            reconcile_timer = self._reconcile_timer
            health_timer = self._health_timer
            self._timer = None
            self._reconcile_timer = None
            self._health_timer = None
            self._last_health_check = None
            self._pending_since = None
            self._pending_paths.clear()
            self._full_rescan = False
        if timer is not None:
            timer.cancel()
        if reconcile_timer is not None:
            reconcile_timer.cancel()
        if health_timer is not None:
            health_timer.cancel()

    def _queue_path(self, path: Path) -> None:
        with self._lock:
            self._pending_paths.add(str(_safe_resolve(path)))
            self._restart_timer_locked()

    def _queue_rescan(self) -> None:
        with self._lock:
            self._full_rescan = True
            self._restart_timer_locked()

    def _restart_timer_locked(self) -> None:
        now = time.monotonic()
        if self._pending_since is None:
            self._pending_since = now
        elapsed = max(0.0, now - self._pending_since)
        remaining = max(0.0, self.max_batch_delay_seconds - elapsed)
        delay = min(self.debounce_seconds, remaining)

        if self._timer is not None:
            self._timer.cancel()
        timer = threading.Timer(delay, self._flush)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _schedule_reconcile_locked(self) -> None:
        if self.reconcile_seconds <= 0 or self._observer is None or self._config is None:
            return
        if self._reconcile_timer is not None:
            self._reconcile_timer.cancel()
        timer = threading.Timer(self.reconcile_seconds, self._periodic_reconcile)
        timer.daemon = True
        self._reconcile_timer = timer
        timer.start()

    def _schedule_health_check_locked(self) -> None:
        if self.health_check_seconds <= 0 or self._observer is None or self._config is None:
            return
        if self._health_timer is not None:
            self._health_timer.cancel()
        timer = threading.Timer(self.health_check_seconds, self._health_check)
        timer.daemon = True
        self._health_timer = timer
        timer.start()

    def _periodic_reconcile(self) -> None:
        with self._lock:
            self._reconcile_timer = None
            if self._observer is None or self._config is None:
                return
            self._full_rescan = True
            self._restart_timer_locked()
            self._schedule_reconcile_locked()

    def _health_check(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._health_timer = None
            observer = self._observer
            config = self._config
            previous_check = self._last_health_check
            self._last_health_check = now

        if observer is None or config is None:
            return

        try:
            observer_alive = observer.is_alive()
        except Exception:
            observer_alive = False

        if not observer_alive:
            roots, excluded = config
            # ``start`` deliberately does not trust a same-config observer once
            # its thread has died. Recreate the native subscription and then
            # reconcile because notifications may have been lost before death.
            self.start(list(roots), list(excluded))
            self._queue_rescan()
            return

        if previous_check is not None and now - previous_check >= self.resume_gap_seconds:
            # Timers normally wake roughly on schedule. A much larger monotonic
            # gap strongly suggests suspend/resume or prolonged process stall.
            # Keep the live observer, but distrust the event history across the
            # gap and reconcile the indexed roots once.
            self._queue_rescan()

        with self._lock:
            if self._observer is observer and self._config == config:
                self._schedule_health_check_locked()

    def _flush(self) -> None:
        with self._lock:
            paths = tuple(sorted(self._pending_paths))
            full_rescan = self._full_rescan
            self._pending_paths.clear()
            self._full_rescan = False
            self._pending_since = None
            self._timer = None

        if full_rescan or paths:
            self.on_change(WatchBatch(paths=paths, full_rescan=full_rescan))
