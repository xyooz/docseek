from __future__ import annotations

import json
from pathlib import Path

from .search_db import SearchDatabase


PAUSED_INDEX_ROOTS_KEY = "paused_index_roots_v1"


class IndexRootStateStore:
    """Persist per-root pause state without changing the index schema.

    Pausing a root only stops refresh/watcher updates. Existing indexed rows are
    intentionally left searchable until the root is resumed or removed.
    """

    def __init__(self, database: SearchDatabase) -> None:
        self.database = database

    @staticmethod
    def _normalize(root: str | Path) -> str:
        return str(Path(root).resolve())

    def _configured_paused(self) -> list[str]:
        raw = self.database._get_setting(PAUSED_INDEX_ROOTS_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []

        result: list[str] = []
        seen: set[str] = set()
        for item in data:
            if not item:
                continue
            normalized = self._normalize(str(item))
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    def paused_roots(self) -> list[str]:
        roots = self.database.get_index_roots()
        paused = set(self._configured_paused())
        return [root for root in roots if root in paused]

    def active_roots(self) -> list[str]:
        paused = set(self.paused_roots())
        return [root for root in self.database.get_index_roots() if root not in paused]

    def is_paused(self, root: str | Path) -> bool:
        return self._normalize(root) in set(self.paused_roots())

    def replace_paused(
        self,
        paused_roots: list[str] | set[str],
        *,
        known_roots: list[str] | None = None,
    ) -> None:
        roots = [
            self._normalize(root)
            for root in (known_roots if known_roots is not None else self.database.get_index_roots())
        ]
        paused = {self._normalize(root) for root in paused_roots}
        ordered = [root for root in roots if root in paused]
        self.database._set_setting(
            PAUSED_INDEX_ROOTS_KEY,
            json.dumps(ordered, ensure_ascii=False),
        )

    def set_paused(self, root: str | Path, paused: bool) -> bool:
        resolved = self._normalize(root)
        roots = self.database.get_index_roots()
        configured = set(self._configured_paused())

        if paused:
            if resolved not in roots:
                return False
            configured.add(resolved)
        else:
            configured.discard(resolved)

        self.replace_paused(list(configured), known_roots=roots)
        return resolved in roots
