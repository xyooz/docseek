from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


_SIZE_RE = re.compile(r"^(>=|<=|>|<)?\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB)?$", re.IGNORECASE)
_SIZE_FACTORS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
}


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    terms: tuple[str, ...]
    extension: str | None = None
    path_contains: str | None = None
    modified_after: float | None = None
    modified_before: float | None = None
    min_size: int | None = None
    max_size: int | None = None

    @property
    def text(self) -> str:
        """Serialize parsed terms without losing quoted-phrase boundaries.

        `_split_tokens()` intentionally removes user-facing quotes. A quoted
        phrase is therefore represented as a single term containing spaces.
        Re-quoting only those terms preserves the distinction between:

        - `customer manager`   -> two terms joined with AND;
        - `"customer manager"` -> one exact phrase.
        """
        rendered: list[str] = []
        for term in self.terms:
            if any(char.isspace() for char in term):
                rendered.append(f'"{term.replace(chr(34), chr(34) * 2)}"')
            else:
                rendered.append(term)
        return " ".join(rendered)

    @property
    def has_filters(self) -> bool:
        return any(
            value is not None
            for value in (
                self.extension,
                self.path_contains,
                self.modified_after,
                self.modified_before,
                self.min_size,
                self.max_size,
            )
        )


@dataclass(frozen=True, slots=True)
class QueryFilterChip:
    """One effective metadata filter that can be shown as a removable UI chip."""

    key: str
    label: str


def _split_tokens(raw: str) -> list[str]:
    """Split search text while preserving Windows backslashes and quoted spaces."""
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    index = 0

    while index < len(raw):
        char = raw[index]
        if char == '"':
            in_quotes = not in_quotes
            index += 1
            continue
        if char.isspace() and not in_quotes:
            if current:
                tokens.append("".join(current))
                current = []
            index += 1
            continue
        current.append(char)
        index += 1

    if current:
        tokens.append("".join(current))
    return tokens


def _parse_date(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").timestamp()
    except ValueError:
        return None


def _parse_size(value: str) -> tuple[str, int] | None:
    match = _SIZE_RE.match(value.strip())
    if not match:
        return None
    operator = match.group(1) or "="
    number = float(match.group(2))
    unit = (match.group(3) or "B").upper()
    return operator, int(number * _SIZE_FACTORS[unit])


def parse_query(raw: str) -> ParsedQuery:
    """Parse a forgiving Everything-style query language.

    Supported filters:

    - ``ext:pdf`` or ``ext:.pdf``
    - ``path:制度`` or ``path:"D:\\工作资料\\业务 制度"``
    - ``after:2026-01-01``
    - ``before:2026-09-01``
    - ``size:>10MB`` / ``size:<=500KB``
    - quoted phrases such as ``"客户经理"``

    Filters may be used without any keyword, allowing metadata-only browsing.
    Unknown or malformed ``key:value`` tokens remain normal search text so the
    search box never becomes fragile just because a filter was mistyped.
    """
    raw = raw.strip()
    if not raw:
        return ParsedQuery(())

    tokens = _split_tokens(raw)
    terms: list[str] = []
    extension: str | None = None
    path_contains: str | None = None
    modified_after: float | None = None
    modified_before: float | None = None
    min_size: int | None = None
    max_size: int | None = None

    for token in tokens:
        lowered = token.casefold()
        if lowered.startswith("ext:") and len(token) > 4:
            value = token[4:].strip()
            if value:
                extension = value if value.startswith(".") else f".{value}"
                extension = extension.casefold()
                continue

        if lowered.startswith("path:") and len(token) > 5:
            value = token[5:].strip()
            if value:
                path_contains = value
                continue

        if lowered.startswith("after:") and len(token) > 6:
            value = _parse_date(token[6:].strip())
            if value is not None:
                modified_after = value
                continue

        if lowered.startswith("before:") and len(token) > 7:
            value = _parse_date(token[7:].strip())
            if value is not None:
                modified_before = value
                continue

        if lowered.startswith("size:") and len(token) > 5:
            parsed_size = _parse_size(token[5:])
            if parsed_size is not None:
                operator, size = parsed_size
                if operator in {">", ">="}:
                    min_size = size + (1 if operator == ">" else 0)
                elif operator in {"<", "<="}:
                    max_size = size - (1 if operator == "<" and size > 0 else 0)
                else:
                    min_size = size
                    max_size = size
                continue

        if token:
            terms.append(token)

    return ParsedQuery(
        tuple(terms),
        extension=extension,
        path_contains=path_contains,
        modified_after=modified_after,
        modified_before=modified_before,
        min_size=min_size,
        max_size=max_size,
    )


def _format_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value):,} B"
            rounded = round(value, 1)
            if rounded.is_integer():
                return f"{int(rounded)} {unit}"
            return f"{rounded:.1f} {unit}"
        value /= 1024
    return f"{size:,} B"


def query_filter_chips(raw: str) -> tuple[QueryFilterChip, ...]:
    """Return user-facing chips for the effective parsed metadata filters.

    Chips describe effective query state, not every token the user typed. This
    matters for repeated filters such as ``ext:pdf ext:docx`` where only the
    final extension is effective. Size lower/upper bounds are intentionally
    represented as one chip because removing it clears the whole size scope.
    """
    parsed = parse_query(raw)
    chips: list[QueryFilterChip] = []
    if parsed.extension is not None:
        chips.append(
            QueryFilterChip("extension", f"类型 · {parsed.extension.lstrip('.').upper()}")
        )
    if parsed.path_contains is not None:
        chips.append(QueryFilterChip("path", f"路径 · {parsed.path_contains}"))
    if parsed.modified_after is not None:
        chips.append(
            QueryFilterChip("after", f"修改时间 ≥ {_format_date(parsed.modified_after)}")
        )
    if parsed.modified_before is not None:
        chips.append(
            QueryFilterChip("before", f"修改时间 < {_format_date(parsed.modified_before)}")
        )
    if parsed.min_size is not None or parsed.max_size is not None:
        if parsed.min_size is not None and parsed.max_size is not None:
            if parsed.min_size == parsed.max_size:
                label = f"大小 · {_format_size(parsed.min_size)}"
            else:
                label = (
                    f"大小 · {_format_size(parsed.min_size)} ～ "
                    f"{_format_size(parsed.max_size)}"
                )
        elif parsed.min_size is not None:
            label = f"大小 ≥ {_format_size(parsed.min_size)}"
        else:
            label = f"大小 ≤ {_format_size(parsed.max_size or 0)}"
        chips.append(QueryFilterChip("size", label))
    return tuple(chips)


def _quote_filter_value(prefix: str, value: str) -> str:
    if any(char.isspace() for char in value):
        return f'{prefix}:"{value}"'
    return f"{prefix}:{value}"


def remove_query_filter(raw: str, key: str) -> str:
    """Remove one logical metadata filter while preserving search semantics.

    The result is normalized rather than byte-for-byte identical to the input.
    Unknown/malformed filter-like tokens remain normal terms. A size chip clears
    both lower and upper bounds because the UI presents them as one logical
    scope.
    """
    parsed = parse_query(raw)
    parts: list[str] = []
    if parsed.text:
        parts.append(parsed.text)

    if key != "extension" and parsed.extension is not None:
        parts.append(f"ext:{parsed.extension.lstrip('.')}")
    if key != "path" and parsed.path_contains is not None:
        parts.append(_quote_filter_value("path", parsed.path_contains))
    if key != "after" and parsed.modified_after is not None:
        parts.append(f"after:{_format_date(parsed.modified_after)}")
    if key != "before" and parsed.modified_before is not None:
        parts.append(f"before:{_format_date(parsed.modified_before)}")
    if key != "size":
        if parsed.min_size is not None and parsed.max_size == parsed.min_size:
            parts.append(f"size:{parsed.min_size}B")
        else:
            if parsed.min_size is not None:
                parts.append(f"size:>={parsed.min_size}B")
            if parsed.max_size is not None:
                parts.append(f"size:<={parsed.max_size}B")

    return " ".join(part for part in parts if part).strip()
