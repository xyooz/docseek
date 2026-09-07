from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .document_types import KNOWN_DOCUMENT_EXTENSIONS, normalize_extension


ENABLED_INDEX_EXTENSIONS_KEY = "enabled_index_extensions"


@dataclass(slots=True, frozen=True)
class IndexFormatGroup:
    key: str
    label: str
    extensions: tuple[str, ...]


OFFICE_WPS_EXTENSIONS = frozenset(
    {
        ".doc",
        ".docx",
        ".dot",
        ".rtf",
        ".xls",
        ".xlsx",
        ".xlsb",
        ".xlt",
        ".ppt",
        ".pptx",
        ".pps",
        ".wps",
        ".wpt",
        ".et",
        ".ett",
        ".etx",
        ".ettx",
        ".dps",
        ".dpt",
    }
)

# A new DocSeek index starts with the formats most office users actually need.
# Existing databases without this setting retain the historical all-format
# behaviour; SearchDatabase writes this explicit default only for a new index.
DEFAULT_ENABLED_INDEX_EXTENSIONS = OFFICE_WPS_EXTENSIONS

INDEX_FORMAT_GROUPS = (
    IndexFormatGroup(
        "office_wps",
        "Office / WPS",
        (
            ".doc", ".docx", ".dot", ".rtf",
            ".xls", ".xlsx", ".xlsb", ".xlt",
            ".ppt", ".pptx", ".pps",
            ".wps", ".wpt", ".et", ".ett", ".etx", ".ettx", ".dps", ".dpt",
        ),
    ),
    IndexFormatGroup("pdf", "PDF", (".pdf",)),
    IndexFormatGroup(
        "text_data", "文本与表格数据", (".txt", ".md", ".log", ".csv", ".tsv")
    ),
    IndexFormatGroup(
        "web_xml", "网页与 XML", (".html", ".htm", ".xhtml", ".xml")
    ),
    IndexFormatGroup(
        "open_formats", "开放文档与电子书", (".odt", ".ods", ".odp", ".epub")
    ),
    IndexFormatGroup("mail", "邮件与邮箱", (".eml", ".msg", ".mbox", ".pst")),
    IndexFormatGroup("iwork", "Apple iWork", (".pages", ".numbers", ".key")),
)


def normalize_enabled_extensions(extensions: object) -> frozenset[str]:
    if not isinstance(extensions, (list, tuple, set, frozenset)):
        return frozenset()
    normalized = {
        normalize_extension(str(extension))
        for extension in extensions
        if str(extension).strip()
    }
    return frozenset(normalized & KNOWN_DOCUMENT_EXTENSIONS)


def serialize_enabled_extensions(extensions: object) -> str:
    return json.dumps(
        sorted(normalize_enabled_extensions(extensions)),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_enabled_extensions(
    raw: str | None,
    *,
    missing_default: frozenset[str] = KNOWN_DOCUMENT_EXTENSIONS,
) -> frozenset[str]:
    if raw is None:
        return frozenset(missing_default)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return frozenset(missing_default)
    if not isinstance(parsed, list):
        return frozenset(missing_default)
    return normalize_enabled_extensions(parsed)


class _SettingsDatabase(Protocol):
    def _get_setting(self, key: str) -> str | None: ...
    def _set_setting(self, key: str, value: str) -> None: ...


class IndexFormatStore:
    def __init__(self, database: _SettingsDatabase) -> None:
        self.database = database

    def enabled_extensions(self) -> frozenset[str]:
        return decode_enabled_extensions(
            self.database._get_setting(ENABLED_INDEX_EXTENSIONS_KEY)
        )

    def has_explicit_setting(self) -> bool:
        return self.database._get_setting(ENABLED_INDEX_EXTENSIONS_KEY) is not None

    def ensure_new_index_default(self) -> frozenset[str]:
        """Persist the focused default at the first user-created index root.

        A missing setting in a database that already has roots belongs to a
        pre-filter DocSeek version and must retain historical all-format
        behaviour. Callers therefore invoke this only before adding the first
        root of a genuinely new profile.
        """
        if self.has_explicit_setting():
            return self.enabled_extensions()
        return self.set_enabled_extensions(DEFAULT_ENABLED_INDEX_EXTENSIONS)

    def set_enabled_extensions(self, extensions: object) -> frozenset[str]:
        normalized = normalize_enabled_extensions(extensions)
        self.database._set_setting(
            ENABLED_INDEX_EXTENSIONS_KEY,
            serialize_enabled_extensions(normalized),
        )
        return normalized
