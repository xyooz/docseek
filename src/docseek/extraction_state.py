from __future__ import annotations

from enum import StrEnum


class ExtractionStatus(StrEnum):
    """Durable lifecycle states for one document extraction attempt.

    The first v9 rollout persists terminal states and reserves the transient
    states for the durable task queue that will follow. Keeping the vocabulary
    stable now avoids another schema churn when crash-resume is introduced.
    """

    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    INDEXED = "INDEXED"
    NO_TEXT = "NO_TEXT"
    OCR_REQUIRED = "OCR_REQUIRED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


TERMINAL_SUCCESS_STATUSES = frozenset(
    {
        ExtractionStatus.INDEXED,
        ExtractionStatus.NO_TEXT,
        ExtractionStatus.OCR_REQUIRED,
    }
)


def status_for_extraction_result(extension: str, chunk_count: int) -> ExtractionStatus:
    """Classify a parser result without inventing searchable content.

    A textless PDF is materially different from a corrupt PDF: extraction
    succeeded but there is no text layer, so it should be eligible for a future
    OCR lane and should not be re-parsed on every reconciliation scan.
    """
    if int(chunk_count) > 0:
        return ExtractionStatus.INDEXED
    if extension.strip().lower() == ".pdf":
        return ExtractionStatus.OCR_REQUIRED
    return ExtractionStatus.NO_TEXT


def status_for_error_code(error_code: str) -> ExtractionStatus:
    if error_code == "LegacyExtractionTimeout":
        return ExtractionStatus.TIMEOUT
    return ExtractionStatus.FAILED


def unchanged_state_is_complete(
    status: str | ExtractionStatus | None,
    *,
    has_chunk: bool,
) -> bool:
    """Return whether an unchanged file can safely skip another extraction.

    v8 rows upgraded to v9 default to INDEXED. Requiring an actual chunk for
    INDEXED deliberately forces any historical zero-chunk rows through one
    repair extraction, after which they become NO_TEXT/OCR_REQUIRED.
    """
    try:
        value = ExtractionStatus(str(status))
    except (TypeError, ValueError):
        return False

    if value is ExtractionStatus.INDEXED:
        return bool(has_chunk)
    if value in {ExtractionStatus.NO_TEXT, ExtractionStatus.OCR_REQUIRED}:
        return not bool(has_chunk)
    return False
