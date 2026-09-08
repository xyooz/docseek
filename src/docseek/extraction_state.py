from __future__ import annotations

import time
from enum import StrEnum


class ExtractionStatus(StrEnum):
    """Durable lifecycle states for one document extraction attempt."""

    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    INDEXED = "INDEXED"
    NO_TEXT = "NO_TEXT"
    OCR_REQUIRED = "OCR_REQUIRED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"
    INTERRUPTED = "INTERRUPTED"
    QUARANTINED = "QUARANTINED"


TERMINAL_SUCCESS_STATUSES = frozenset(
    {
        ExtractionStatus.INDEXED,
        ExtractionStatus.NO_TEXT,
        ExtractionStatus.OCR_REQUIRED,
    }
)
FAILURE_STATUSES = frozenset({ExtractionStatus.FAILED, ExtractionStatus.TIMEOUT})
MANUAL_DEFERRED_STATUSES = frozenset(
    {
        ExtractionStatus.SKIPPED,
        ExtractionStatus.INTERRUPTED,
        ExtractionStatus.QUARANTINED,
    }
)
DEFERRED_STATUSES = FAILURE_STATUSES | MANUAL_DEFERRED_STATUSES

# Full reconciliation scans should not hammer a permanently broken document.
# Precise watcher/manual retries bypass this policy in DirectoryIndexer.
FAILED_RETRY_BASE_SECONDS = 5 * 60
TIMEOUT_RETRY_BASE_SECONDS = 30 * 60
MAX_RETRY_DELAY_SECONDS = 24 * 60 * 60


def status_for_extraction_result(extension: str, chunk_count: int) -> ExtractionStatus:
    """Classify a parser result without inventing searchable content."""
    if int(chunk_count) > 0:
        return ExtractionStatus.INDEXED
    if extension.strip().lower() == ".pdf":
        return ExtractionStatus.OCR_REQUIRED
    return ExtractionStatus.NO_TEXT


def status_for_error_code(error_code: str) -> ExtractionStatus:
    if error_code == "LegacyExtractionTimeout":
        return ExtractionStatus.TIMEOUT
    if error_code == "ParserInterrupted":
        return ExtractionStatus.INTERRUPTED
    if error_code == "ParserCancelled":
        return ExtractionStatus.SKIPPED
    return ExtractionStatus.FAILED


def retry_delay_seconds(
    status: str | ExtractionStatus,
    failure_count: int,
) -> float:
    """Return a bounded exponential retry delay for a failed extraction."""
    try:
        value = ExtractionStatus(str(status))
    except ValueError:
        return 0.0
    if value not in FAILURE_STATUSES:
        return 0.0

    base = (
        TIMEOUT_RETRY_BASE_SECONDS
        if value is ExtractionStatus.TIMEOUT
        else FAILED_RETRY_BASE_SECONDS
    )
    exponent = max(0, min(int(failure_count) - 1, 8))
    return float(min(base * (2**exponent), MAX_RETRY_DELAY_SECONDS))


def failed_state_is_deferred(
    status: str | ExtractionStatus | None,
    *,
    stored_revision: int,
    current_revision: int,
    source_modified_time: float | None,
    source_size: int | None,
    current_modified_time: float,
    current_size: int,
    retry_after: float,
    now: float | None = None,
) -> bool:
    """Return whether an unchanged failed file should wait before full retry.

    A changed file or newer extractor revision always reactivates immediately.
    This is intentionally used only by full reconciliation scans. Watcher and
    explicit user retries stay precise and immediate.
    """
    try:
        value = ExtractionStatus(str(status))
    except (TypeError, ValueError):
        return False
    if value not in DEFERRED_STATUSES:
        return False
    if int(stored_revision) < int(current_revision):
        return False
    if source_modified_time is None or source_size is None:
        return False
    if float(source_modified_time) != float(current_modified_time):
        return False
    if int(source_size) != int(current_size):
        return False
    # These states are an explicit user/recovery decision, not a timed retry.
    # Keep the unchanged file deferred indefinitely until its metadata changes
    # or a precise manual retry path clears the state.
    if value in MANUAL_DEFERRED_STATUSES:
        return True
    current_time = time.time() if now is None else float(now)
    return current_time < float(retry_after)


def unchanged_state_is_complete(
    status: str | ExtractionStatus | None,
    *,
    has_chunk: bool,
) -> bool:
    """Return whether an unchanged file can safely skip another extraction.

    v8 rows upgraded through v9/v10 default to INDEXED. Requiring an actual
    chunk for INDEXED deliberately forces historical zero-chunk rows through one
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
