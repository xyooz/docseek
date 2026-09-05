from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .extractors import SUPPORTED_EXTENSIONS
from .index_issues import IndexIssue


@dataclass(frozen=True, slots=True)
class IndexRetryPlan:
    """Minimal work needed to retry a set of persisted index problems."""

    file_paths: tuple[Path, ...]
    rescan_roots: tuple[Path, ...]
    skipped_paths: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.file_paths and not self.rescan_roots


def _normalized(path: str | Path) -> Path:
    candidate = Path(path)
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _containing_root(path: Path, roots: tuple[Path, ...]) -> Path | None:
    # Prefer the most specific root when users intentionally index nested
    # directories. This keeps a directory-level retry as small as possible.
    for root in sorted(roots, key=lambda item: len(item.parts), reverse=True):
        if path == root:
            return root
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def build_index_retry_plan(
    issues: Iterable[IndexIssue],
    *,
    active_roots: Iterable[str | Path],
) -> IndexRetryPlan:
    """Classify retries into precise file updates or root reconciliation.

    Issues outside active roots are deliberately skipped. In particular, this
    prevents an explicit pause from being bypassed by the retry UI.

    A path with a supported document extension is retried precisely even when
    the file is currently missing; ``DirectoryIndexer.update_paths`` already
    owns deletion/error recovery semantics. Directory-like and other
    non-document paths require a reconciliation scan of the smallest active
    root that contains them.
    """

    roots = tuple(dict.fromkeys(_normalized(root) for root in active_roots))
    file_paths: list[Path] = []
    rescan_roots: list[Path] = []
    skipped: list[str] = []

    seen_files: set[Path] = set()
    seen_roots: set[Path] = set()
    seen_skipped: set[str] = set()

    for issue in issues:
        path = _normalized(issue.path)
        root = _containing_root(path, roots)
        if root is None:
            if issue.path not in seen_skipped:
                seen_skipped.add(issue.path)
                skipped.append(issue.path)
            continue

        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            if path not in seen_files:
                seen_files.add(path)
                file_paths.append(path)
            continue

        if root not in seen_roots:
            seen_roots.add(root)
            rescan_roots.append(root)

    # If a root will be reconciled anyway, targeted updates underneath it are
    # redundant. Drop them so one user action never schedules duplicate work.
    precise = [
        path
        for path in file_paths
        if _containing_root(path, tuple(rescan_roots)) is None
    ]

    return IndexRetryPlan(
        file_paths=tuple(precise),
        rescan_roots=tuple(rescan_roots),
        skipped_paths=tuple(skipped),
    )


def dispatch_index_retry(window, plan: IndexRetryPlan) -> None:
    """Route a retry plan through the desktop's established index workers.

    The helper intentionally uses the same full-scan and precise-update entry
    points as normal watcher/refresh work. When both are needed, precise file
    retries are placed on the existing pending-path queue and drain after the
    root reconciliation finishes, so two SQLite writers are never launched in
    parallel by one retry action.
    """

    skipped = len(plan.skipped_paths)
    if plan.empty:
        if skipped:
            window.statusBar().showMessage(
                f"有 {skipped} 项位于已暂停或已移除的索引目录，未执行重试",
                7000,
            )
        else:
            window.statusBar().showMessage("当前没有可重试的索引问题", 5000)
        return

    if plan.rescan_roots:
        window.pending_watch_paths.update(str(path) for path in plan.file_paths)
        window._start_index(list(plan.rescan_roots))
        message = f"正在重试索引问题：校准 {len(plan.rescan_roots)} 个目录"
        if plan.file_paths:
            message += f"，随后重试 {len(plan.file_paths)} 个文件"
    else:
        window._start_path_update(list(plan.file_paths))
        message = f"正在重试 {len(plan.file_paths)} 个问题文件"

    if skipped:
        message += f"；另有 {skipped} 项因目录暂停或已移除而跳过"
    window.statusBar().showMessage(message, 7000)
