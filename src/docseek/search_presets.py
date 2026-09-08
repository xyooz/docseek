from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .search_sort import SORT_FILENAME, SORT_MODIFIED, SORT_RELEVANCE, validate_sort_mode


@dataclass(frozen=True, slots=True)
class SearchState:
    """A restorable desktop search state.

    Advanced filters remain embedded in ``query`` while the two explicit UI
    selectors are persisted separately. This reproduces the exact effective
    search without coupling history storage to query-parser internals.
    """

    query: str = ""
    extension: str | None = None
    sort_mode: str = SORT_RELEVANCE

    @property
    def meaningful(self) -> bool:
        return bool(self.query.strip() or self.extension)

    @property
    def key(self) -> tuple[str, str | None, str]:
        return (self.query.strip(), self.extension, self.sort_mode)

    def normalized(self) -> SearchState:
        extension = self.extension.strip().lower() if self.extension else None
        if extension and not extension.startswith((".", "@")):
            extension = f".{extension}"
        return SearchState(
            query=self.query.strip(),
            extension=extension,
            sort_mode=validate_sort_mode(self.sort_mode),
        )

    @classmethod
    def from_mapping(cls, value: object) -> SearchState | None:
        if not isinstance(value, dict):
            return None
        try:
            query = value.get("query", "")
            extension = value.get("extension")
            sort_mode = value.get("sort_mode", SORT_RELEVANCE)
            if not isinstance(query, str):
                return None
            if extension is not None and not isinstance(extension, str):
                return None
            if not isinstance(sort_mode, str):
                return None
            state = cls(query=query, extension=extension, sort_mode=sort_mode).normalized()
        except (ValueError, AttributeError):
            return None
        return state if state.meaningful else None


def search_state_label(state: SearchState, *, max_query_chars: int = 44) -> str:
    state = state.normalized()
    query = state.query or "全部内容"
    if len(query) > max_query_chars:
        query = query[: max_query_chars - 1] + "…"

    details: list[str] = []
    if state.extension:
        from .format_filters import FORMAT_FAMILIES
        family = FORMAT_FAMILIES.get(state.extension)
        details.append(family[0] if family else state.extension.lstrip(".").upper())
    if state.sort_mode == SORT_MODIFIED:
        details.append("最近修改")
    elif state.sort_mode == SORT_FILENAME:
        details.append("文件名排序")

    return f"{query}  ·  {' · '.join(details)}" if details else query


class SearchStateStore:
    """Persist recent and explicitly saved searches in the existing settings table."""

    HISTORY_KEY = "search_history_v1"
    SAVED_KEY = "saved_searches_v1"
    MAX_HISTORY = 20
    MAX_SAVED = 30

    def __init__(self, database) -> None:
        self.database = database

    def _read(self, key: str, *, limit: int) -> list[SearchState]:
        raw = self.database._get_setting(key)
        if not raw:
            return []
        try:
            values = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(values, list):
            return []

        states: list[SearchState] = []
        seen: set[tuple[str, str | None, str]] = set()
        for value in values:
            state = SearchState.from_mapping(value)
            if state is None or state.key in seen:
                continue
            seen.add(state.key)
            states.append(state)
            if len(states) >= limit:
                break
        return states

    def _write(self, key: str, states: list[SearchState]) -> None:
        payload = [asdict(state.normalized()) for state in states]
        self.database._set_setting(key, json.dumps(payload, ensure_ascii=False))

    def history(self) -> list[SearchState]:
        return self._read(self.HISTORY_KEY, limit=self.MAX_HISTORY)

    def record_history(self, state: SearchState) -> None:
        state = state.normalized()
        if not state.meaningful:
            return
        items = [item for item in self.history() if item.key != state.key]
        items.insert(0, state)
        self._write(self.HISTORY_KEY, items[: self.MAX_HISTORY])

    def clear_history(self) -> None:
        self._write(self.HISTORY_KEY, [])

    def saved(self) -> list[SearchState]:
        return self._read(self.SAVED_KEY, limit=self.MAX_SAVED)

    def is_saved(self, state: SearchState) -> bool:
        try:
            key = state.normalized().key
        except ValueError:
            return False
        return any(item.key == key for item in self.saved())

    def toggle_saved(self, state: SearchState) -> bool:
        """Toggle a saved search and return True when it is saved afterwards."""
        state = state.normalized()
        if not state.meaningful:
            return False
        items = self.saved()
        if any(item.key == state.key for item in items):
            self._write(self.SAVED_KEY, [item for item in items if item.key != state.key])
            return False
        items.insert(0, state)
        self._write(self.SAVED_KEY, items[: self.MAX_SAVED])
        return True
