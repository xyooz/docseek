from __future__ import annotations


BASE_EXTRACTION_REVISION = 1

# Bump only the formats whose extraction semantics changed. This keeps a
# reconciliation scan selective instead of turning every extractor improvement
# into a full-index rebuild.
_FORMAT_REVISIONS = {
    ".docx": 3,  # preserve body/table order and split at heading boundaries
    ".pptx": 2,  # preserve slide titles in slide locations
    ".html": 2,  # direct visible-text parser replaces per-file Tika process
    ".htm": 2,
    ".xhtml": 2,
    ".xml": 2,  # direct streaming parser replaces compatibility-only extraction
    ".tsv": 2,  # direct text parser replaces flat Tika extraction
}


def current_extraction_revision(extension: str) -> int:
    value = extension.strip().lower()
    if value and not value.startswith("."):
        value = f".{value}"
    return _FORMAT_REVISIONS.get(value, BASE_EXTRACTION_REVISION)
