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
        return " ".join(self.terms)


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
