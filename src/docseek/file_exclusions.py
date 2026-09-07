from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Iterable

from .search_db import SearchDatabase


FILE_EXCLUSION_PATTERNS_KEY = "excluded_file_patterns_v1"
MAX_FILE_EXCLUSION_PATTERNS = 100
MAX_FILE_EXCLUSION_PATTERN_LENGTH = 120
_DANGEROUS_ALL_PATTERNS = {"*", "*.*"}


class InvalidFileExclusionPattern(ValueError):
    pass


def normalize_file_exclusion_pattern(raw: str) -> str:
    pattern = str(raw).strip()
    if not pattern:
        raise InvalidFileExclusionPattern("排除规则不能为空")
    if len(pattern) > MAX_FILE_EXCLUSION_PATTERN_LENGTH:
        raise InvalidFileExclusionPattern(
            f"单条排除规则不能超过 {MAX_FILE_EXCLUSION_PATTERN_LENGTH} 个字符"
        )
    if "/" in pattern or "\\" in pattern:
        raise InvalidFileExclusionPattern(
            "排除规则只匹配文件名，请不要填写目录或路径；目录请使用“排除目录”"
        )
    if pattern.casefold() in _DANGEROUS_ALL_PATTERNS:
        raise InvalidFileExclusionPattern(
            "不能使用 * 或 *.* 排除全部文件；如需停止更新，请暂停索引目录"
        )

    # A leading-dot shorthand is convenient for normal users: `.log` means
    # the same thing as `*.log`. Exact filenames without wildcards remain exact.
    if pattern.startswith(".") and not any(char in pattern for char in "*?["):
        pattern = f"*{pattern}"
    return pattern


def normalize_file_exclusion_patterns(patterns: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in patterns:
        if not str(raw).strip():
            continue
        pattern = normalize_file_exclusion_pattern(str(raw))
        key = pattern.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(pattern)
        if len(normalized) > MAX_FILE_EXCLUSION_PATTERNS:
            raise InvalidFileExclusionPattern(
                f"最多支持 {MAX_FILE_EXCLUSION_PATTERNS} 条排除规则"
            )
    return normalized


def decode_file_exclusion_patterns(raw: str | None) -> list[str]:
    """Read persisted patterns defensively so malformed settings never block indexing."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []

    valid: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        try:
            pattern = normalize_file_exclusion_pattern(str(item))
        except InvalidFileExclusionPattern:
            continue
        key = pattern.casefold()
        if key in seen:
            continue
        seen.add(key)
        valid.append(pattern)
        if len(valid) >= MAX_FILE_EXCLUSION_PATTERNS:
            break
    return valid


def matches_file_exclusion(path: str | Path, patterns: Iterable[str]) -> bool:
    name = Path(path).name.casefold()
    return any(fnmatch.fnmatchcase(name, str(pattern).casefold()) for pattern in patterns)


class FileExclusionStore:
    """Persist simple filename glob exclusions without changing the DB schema."""

    def __init__(self, database: SearchDatabase) -> None:
        self.database = database

    def patterns(self) -> list[str]:
        return decode_file_exclusion_patterns(
            self.database._get_setting(FILE_EXCLUSION_PATTERNS_KEY)
        )

    def set_patterns(self, patterns: Iterable[str]) -> list[str]:
        normalized = normalize_file_exclusion_patterns(patterns)
        self.database._set_setting(
            FILE_EXCLUSION_PATTERNS_KEY,
            json.dumps(normalized, ensure_ascii=False),
        )
        return normalized
