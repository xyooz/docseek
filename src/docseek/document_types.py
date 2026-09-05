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
    TIKA_NATIVE = "tika-native"
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
    # Lightweight textual formats stay on the direct streaming path.
    FormatCapability(".txt", DocumentFamily.TEXT, (SupportMode.DIRECT,), "Text"),
    FormatCapability(".md", DocumentFamily.TEXT, (SupportMode.DIRECT,), "Markdown"),
    FormatCapability(".log", DocumentFamily.TEXT, (SupportMode.DIRECT,), "Log"),
    FormatCapability(".csv", DocumentFamily.TEXT, (SupportMode.DIRECT,), "CSV"),

    # Mature structure-preserving parsers for the most common office formats.
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

    # Calamine preserves spreadsheet row/sheet structure and is therefore the
    # preferred backend for legacy/binary/OpenDocument workbooks. Tika and WPS
    # provide independent compatibility paths where available.
    FormatCapability(
        ".xls",
        DocumentFamily.SPREADSHEET,
        (SupportMode.CALAMINE, SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "Excel 97-2003",
    ),
    FormatCapability(
        ".xlsb",
        DocumentFamily.SPREADSHEET,
        (SupportMode.CALAMINE, SupportMode.TIKA_NATIVE),
        "Excel Binary Workbook",
    ),
    FormatCapability(
        ".ods",
        DocumentFamily.SPREADSHEET,
        (SupportMode.CALAMINE, SupportMode.TIKA_NATIVE),
        "OpenDocument Spreadsheet",
    ),

    # Native Tika is the broad compatibility layer. It gives DocSeek one
    # isolated extraction interface for legacy Office, OpenDocument, web,
    # e-book, e-mail and iWork content instead of maintaining bespoke parsers.
    FormatCapability(
        ".doc",
        DocumentFamily.WRITER,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "Word 97-2003",
    ),
    FormatCapability(
        ".dot",
        DocumentFamily.WRITER,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "Word Template",
    ),
    FormatCapability(
        ".rtf",
        DocumentFamily.WRITER,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "Rich Text Format",
    ),
    FormatCapability(
        ".odt", DocumentFamily.WRITER, (SupportMode.TIKA_NATIVE,), "OpenDocument Text"
    ),
    FormatCapability(
        ".ppt",
        DocumentFamily.PRESENTATION,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "PowerPoint 97-2003",
    ),
    FormatCapability(
        ".pps",
        DocumentFamily.PRESENTATION,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "PowerPoint Show",
    ),
    FormatCapability(
        ".odp", DocumentFamily.PRESENTATION, (SupportMode.TIKA_NATIVE,), "OpenDocument Presentation"
    ),

    # Additional formats explicitly covered by the bundled native Tika layer.
    FormatCapability(".tsv", DocumentFamily.TEXT, (SupportMode.TIKA_NATIVE,), "TSV"),
    FormatCapability(".html", DocumentFamily.TEXT, (SupportMode.TIKA_NATIVE,), "HTML"),
    FormatCapability(".htm", DocumentFamily.TEXT, (SupportMode.TIKA_NATIVE,), "HTML"),
    FormatCapability(".xhtml", DocumentFamily.TEXT, (SupportMode.TIKA_NATIVE,), "XHTML"),
    FormatCapability(".xml", DocumentFamily.TEXT, (SupportMode.TIKA_NATIVE,), "XML"),
    FormatCapability(".epub", DocumentFamily.WRITER, (SupportMode.TIKA_NATIVE,), "EPUB"),
    FormatCapability(".eml", DocumentFamily.WRITER, (SupportMode.TIKA_NATIVE,), "E-mail Message"),
    FormatCapability(".msg", DocumentFamily.WRITER, (SupportMode.TIKA_NATIVE,), "Outlook Message"),
    FormatCapability(".mbox", DocumentFamily.WRITER, (SupportMode.TIKA_NATIVE,), "Mailbox"),
    FormatCapability(".pst", DocumentFamily.WRITER, (SupportMode.TIKA_NATIVE,), "Outlook Data File"),

    # Apache Tika exposes iWork package parsers. Keep these in the isolated
    # compatibility lane because package versions differ widely in practice.
    FormatCapability(".pages", DocumentFamily.WRITER, (SupportMode.TIKA_NATIVE,), "Apple Pages"),
    FormatCapability(".numbers", DocumentFamily.SPREADSHEET, (SupportMode.TIKA_NATIVE,), "Apple Numbers"),
    FormatCapability(".key", DocumentFamily.PRESENTATION, (SupportMode.TIKA_NATIVE,), "Apple Keynote"),

    # Real WPS/ET/DPS samples supplied for DocSeek's regression corpus are
    # OLE/CFB compound documents with recognizable WordDocument/Workbook/
    # PowerPoint streams. Try the local native-Tika compatibility parser first;
    # if it cannot extract a particular WPS variant, the installed WPS client
    # remains the vendor fallback. Template variants stay vendor-only until we
    # have representative fixtures proving the same compatibility path.
    FormatCapability(
        ".wps",
        DocumentFamily.WRITER,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "WPS Writer",
    ),
    FormatCapability(".wpt", DocumentFamily.WRITER, (SupportMode.WPS_LOCAL,), "WPS Writer Template"),
    FormatCapability(
        ".et",
        DocumentFamily.SPREADSHEET,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "WPS Spreadsheet",
    ),
    FormatCapability(".ett", DocumentFamily.SPREADSHEET, (SupportMode.WPS_LOCAL,), "WPS Spreadsheet Template"),
    FormatCapability(
        ".xlt",
        DocumentFamily.SPREADSHEET,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "Excel Template",
    ),
    FormatCapability(
        ".dps",
        DocumentFamily.PRESENTATION,
        (SupportMode.TIKA_NATIVE, SupportMode.WPS_LOCAL),
        "WPS Presentation",
    ),
    FormatCapability(".dpt", DocumentFamily.PRESENTATION, (SupportMode.WPS_LOCAL,), "WPS Presentation Template"),
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
TIKA_NATIVE_CANDIDATE_EXTENSIONS = frozenset(
    capability.extension
    for capability in _FORMATS
    if SupportMode.TIKA_NATIVE in capability.modes
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
