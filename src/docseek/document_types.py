from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DocumentFamily(StrEnum):
    TEXT = "text"
    WRITER = "writer"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    PDF = "pdf"


class SupportMode(StrEnum):
    DIRECT = "direct"
    WPS_LOCAL = "wps-local"


@dataclass(slots=True, frozen=True)
class FormatCapability:
    extension: str
    family: DocumentFamily
    mode: SupportMode
    label: str


_DIRECT_FORMATS = (
    FormatCapability(".txt", DocumentFamily.TEXT, SupportMode.DIRECT, "Text"),
    FormatCapability(".md", DocumentFamily.TEXT, SupportMode.DIRECT, "Markdown"),
    FormatCapability(".log", DocumentFamily.TEXT, SupportMode.DIRECT, "Log"),
    FormatCapability(".csv", DocumentFamily.TEXT, SupportMode.DIRECT, "CSV"),
    FormatCapability(".docx", DocumentFamily.WRITER, SupportMode.DIRECT, "Word / WPS Writer"),
    FormatCapability(".xlsx", DocumentFamily.SPREADSHEET, SupportMode.DIRECT, "Excel / WPS Spreadsheet"),
    FormatCapability(".pptx", DocumentFamily.PRESENTATION, SupportMode.DIRECT, "PowerPoint / WPS Presentation"),
    FormatCapability(".pdf", DocumentFamily.PDF, SupportMode.DIRECT, "PDF"),
)

# These formats are deliberately registered as WPS-local candidates instead of
# being added to DIRECT_SUPPORTED_EXTENSIONS. DocSeek must not silently claim a
# format is indexable unless the local WPS adapter can actually handle it.
_WPS_LOCAL_FORMATS = (
    FormatCapability(".wps", DocumentFamily.WRITER, SupportMode.WPS_LOCAL, "WPS Writer"),
    FormatCapability(".wpt", DocumentFamily.WRITER, SupportMode.WPS_LOCAL, "WPS Writer Template"),
    FormatCapability(".doc", DocumentFamily.WRITER, SupportMode.WPS_LOCAL, "Word 97-2003"),
    FormatCapability(".dot", DocumentFamily.WRITER, SupportMode.WPS_LOCAL, "Word Template"),
    FormatCapability(".rtf", DocumentFamily.WRITER, SupportMode.WPS_LOCAL, "Rich Text Format"),
    FormatCapability(".et", DocumentFamily.SPREADSHEET, SupportMode.WPS_LOCAL, "WPS Spreadsheet"),
    FormatCapability(".ett", DocumentFamily.SPREADSHEET, SupportMode.WPS_LOCAL, "WPS Spreadsheet Template"),
    FormatCapability(".xls", DocumentFamily.SPREADSHEET, SupportMode.WPS_LOCAL, "Excel 97-2003"),
    FormatCapability(".xlt", DocumentFamily.SPREADSHEET, SupportMode.WPS_LOCAL, "Excel Template"),
    FormatCapability(".dps", DocumentFamily.PRESENTATION, SupportMode.WPS_LOCAL, "WPS Presentation"),
    FormatCapability(".dpt", DocumentFamily.PRESENTATION, SupportMode.WPS_LOCAL, "WPS Presentation Template"),
    FormatCapability(".ppt", DocumentFamily.PRESENTATION, SupportMode.WPS_LOCAL, "PowerPoint 97-2003"),
    FormatCapability(".pps", DocumentFamily.PRESENTATION, SupportMode.WPS_LOCAL, "PowerPoint Show"),
)

FORMAT_CAPABILITIES = {
    capability.extension: capability
    for capability in (*_DIRECT_FORMATS, *_WPS_LOCAL_FORMATS)
}

DIRECT_SUPPORTED_EXTENSIONS = frozenset(
    capability.extension for capability in _DIRECT_FORMATS
)
WPS_LOCAL_CANDIDATE_EXTENSIONS = frozenset(
    capability.extension for capability in _WPS_LOCAL_FORMATS
)


def normalize_extension(extension: str) -> str:
    value = extension.strip().lower()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


def get_format_capability(extension: str) -> FormatCapability | None:
    return FORMAT_CAPABILITIES.get(normalize_extension(extension))


def document_family_for_extension(extension: str) -> DocumentFamily | None:
    capability = get_format_capability(extension)
    return capability.family if capability is not None else None
