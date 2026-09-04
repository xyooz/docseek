from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    terms: tuple[str, ...]
    extension: str | None = None
    path_contains: str | None = None

    @property
    def text(self) -> str:
        return " ".join(self.terms)


def parse_query(raw: str) -> ParsedQuery:
    """Parse a small Everything-style query language.

    Supported filters are intentionally conservative so ordinary users can
    still type plain keywords without learning syntax:

    - ``ext:pdf`` or ``ext:.pdf``
    - ``path:制度`` or ``path:"业务 制度"``
    - quoted phrases such as ``"客户经理"``

    Unknown ``key:value`` tokens are treated as normal search text rather than
    rejected, keeping the search box forgiving.
    """
    raw = raw.strip()
    if not raw:
        return ParsedQuery(())

    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        # Unmatched quotes should not make the whole search box unusable.
        tokens = raw.split()

    terms: list[str] = []
    extension: str | None = None
    path_contains: str | None = None

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
        if token:
            terms.append(token)

    return ParsedQuery(tuple(terms), extension=extension, path_contains=path_contains)
