from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StructureHints:
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None

    @property
    def active(self) -> bool:
        return self.page is not None or self.slide is not None or self.sheet is not None


@dataclass(frozen=True, slots=True)
class ParsedStructureQuery:
    text: str
    hints: StructureHints



def _split_tokens(raw: str) -> list[tuple[str, bool]]:
    """Split text while preserving quoted spans and Windows-style backslashes."""
    tokens: list[tuple[str, bool]] = []
    current: list[str] = []
    in_quotes = False
    token_quoted = False

    def flush() -> None:
        nonlocal current, token_quoted
        if current:
            tokens.append(("".join(current), token_quoted))
            current = []
            token_quoted = False

    for char in raw.strip():
        if char == '"':
            if not current:
                token_quoted = True
            in_quotes = not in_quotes
            continue
        if char.isspace() and not in_quotes:
            flush()
            continue
        current.append(char)
    flush()
    return tokens



def parse_structure_query(raw: str) -> ParsedStructureQuery:
    """Extract schema-free structure hints from a normal content query.

    Supported hints are ``page:N``, ``slide:N`` and ``sheet:name``. Hints are
    activated only when at least one normal content term remains. That rule
    keeps structure-only input backward-compatible until DocSeek has a proper
    browse-by-structure path.
    """
    tokens = _split_tokens(raw)
    content: list[tuple[str, bool]] = []
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None

    for token, quoted in tokens:
        lowered = token.casefold()
        if lowered.startswith("page:") and len(token) > 5:
            value = token[5:].strip()
            if value.isdigit() and int(value) > 0:
                page = int(value)
                continue
        if lowered.startswith("slide:") and len(token) > 6:
            value = token[6:].strip()
            if value.isdigit() and int(value) > 0:
                slide = int(value)
                continue
        if lowered.startswith("sheet:") and len(token) > 6:
            value = token[6:].strip()
            if value:
                sheet = value
                continue
        content.append((token, quoted))

    if not content:
        return ParsedStructureQuery(raw.strip(), StructureHints())

    rendered: list[str] = []
    for token, quoted in content:
        escaped = token.replace('"', '""')
        if quoted or any(char.isspace() for char in token):
            rendered.append(f'"{escaped}"')
        else:
            rendered.append(token)

    return ParsedStructureQuery(
        " ".join(rendered),
        StructureHints(page=page, slide=slide, sheet=sheet),
    )



def location_boost(location: str, hints: StructureHints) -> float:
    """Return a small additive score for a matching structural locator.

    DocSeek ranks lower scores first. Values are intentionally coarse and only
    serve as a strong tie/intent signal relative to BM25; they do not encode a
    probabilistic relevance model.
    """
    compact = "".join(location.split())
    score = 0.0

    if hints.page is not None and compact == f"第{hints.page}页":
        score -= 3.0

    if hints.slide is not None and compact == f"幻灯片{hints.slide}":
        score -= 3.0

    if hints.sheet is not None:
        prefix = f"工作表{''.join(hints.sheet.split()).casefold()}·行"
        if compact.casefold().startswith(prefix):
            score -= 2.5

    return score
