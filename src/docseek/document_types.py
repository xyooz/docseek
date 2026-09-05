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
    CALAMINE = "calamine"
    WPS_LOCAL = "wps-local"


@dataclass(slots=True, frozen=True)
class FormatCapability:
    extension: str
    family: DocumentFamily
    modes: tuple[SupportMode, ...]
    label: str

    @property
    def preferred_mode(self) -> SupportMode:
        return self.modes[0]


_FORMATS = (
    FormatCapability(".txt", DocumentFamily.TEXT, (SupportMode.DIRECT,), "Text"),
    FormatCapability(".md", DocumentFamily.TEXT, (SupportMode.DIRECT,), "Markdown"),
    FormatCapability(".log", DocumentFamily.TEXT, (SupportMode.DIRECT,), "Log"),
    FormatCapability(".csv", DocumentFamily.TEXT, (SupportMode.DIRECT,), "CSV"),
    FormatCapability(
        ".docx", DocumentFamily.WRITER, (SupportMode.DIRECT,), "Word / WPS Writer"
    ),
    FormatCapability(
        ".xlsx", DocumentFamily.SPREADSHEET, (SupportMode.DIRECT,), "Excel / WPS Spreadsheet"
    ),
    FormatCapability(
        ".pptx", DocumentFamily.PRESENTATION, (SupportMode.DIRECT,), "PowerPoint / WPS Presentation"
    ),
    FormatCapability(".pdf", DocumentFamily.PDF, (SupportMode.DIRECT,), "PDF"),
    # Mature Rust-backed spreadsheet parsing is preferred for legacy/binary and
    # OpenDocument workbooks. WPS remains a local fallback for .xls where the
    # client is available, but DocSeek does not implement the binary format.
    FormatCapability(
        ".xls",
        DocumentFamily.SPREADSHEET,
        (SupportMode.CALAMINE, SupportMode.WPS_LOCAL),
        "Excel 97-2003",
    ),
    FormatCapability(
        ".xlsb", DocumentFamily.SPREADSHEET, (SupportMode.CALAMINE,), "Excel Binary Workbook"
    ),
    FormatCapability(
        ".ods", DocumentFamily.SPREADSHEET, (SupportMode.CALAMINE,), "OpenDocument Spreadsheet"
    ),
    # WPS native and legacy Writer/Presentation formats use the installed WPS
    # client as a thin local compatibility bridge. They are known formats, not
    # unconditional support claims: runtime availability is checked separately.
    FormatCapability(".wps", DocumentFamily.WRITER, (SupportMode.WPS_LOCAL,), "WPS Writer"),
    FormatCapability(".wpt", DocumentFamily.WRITER, (SupportMode.WPS_LOCAL,), "WPS Writer Template"),
    FormatCapability(".doc", DocumentFamily.WRITER, (SupportMode.WPS_LOCAL,), "Word 97-2003"),
    FormatCapability(".dot", DocumentFamily.WRITER, (SupportMode.WPS_LOCAL,), "Word Template"),
    FormatCapability(".rtf", DocumentFamily.WRITER, (SupportMode.WPS_LOCAL,), "Rich Text Format"),
    FormatCapability(".et", DocumentFamily.SPREADSHEET, (SupportMode.WPS_LOCAL,), "WPS Spreadsheet"),
    FormatCapability(".ett", DocumentFamily.SPREADSHEET, (SupportMode.WPS_LOCAL,), "WPS Spreadsheet Template"),
    FormatCapability(".xlt", DocumentFamily.SPREADSHEET, (SupportMode.WPS_LOCAL,), "Excel Template"),
    FormatCapability(".dps", DocumentFamily.PRESENTATION, (SupportMode.WPS_LOCAL,), "WPS Presentation"),
    FormatCapability(".dpt", DocumentFamily.PRESENTATION, (SupportMode.WPS_LOCAL,), "WPS Presentation Template"),
    FormatCapability(".ppt", DocumentFamily.PRESENTATION, (SupportMode.WPS_LOCAL,), "PowerPoint 97-2003"),
    FormatCapability(".pps", DocumentFamily.PRESENTATION, (SupportMode.WPS_LOCAL,), "PowerPoint Show"),
)

FORMAT_CAPABILITIES = {capability.extension: capability for capability in _FORMATS}

DIRECT_SUPPORTED_EXTENSIONS = frozenset(
    capability.extension
    for capability in _FORMATS
    if SupportMode.DIRECT in capability.modes
)
CALAMINE_CANDIDATE_EXTENSIONS = frozenset(
    capability.extension
    for capability in _FORMATS
    if SupportMode.CALAMINE in capability.modes
)
WPS_LOCAL_CANDIDATE_EXTENSIONS = frozenset(
    capability.extension
    for capability in _FORMATS
    if SupportMode.WPS_LOCAL in capability.modes
)
KNOWN_DOCUMENT_EXTENSIONS = frozenset(FORMAT_CAPABILITIES)


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
