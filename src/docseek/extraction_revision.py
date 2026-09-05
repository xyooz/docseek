from __future__ import annotations


BASE_EXTRACTION_REVISION = 1

# Bump only the formats whose extraction semantics changed. This keeps a
# reconciliation scan selective instead of turning every extractor improvement
# into a full-index rebuild.
_FORMAT_REVISIONS = {
    ".docx": 2,  # preserve heading titles in writer block locations
    ".pptx": 2,  # preserve slide titles in slide locations
}


def current_extraction_revision(extension: str) -> int:
    value = extension.strip().lower()
    if value and not value.startswith("."):
        value = f".{value}"
    return _FORMAT_REVISIONS.get(value, BASE_EXTRACTION_REVISION)
