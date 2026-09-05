from __future__ import annotations

SORT_RELEVANCE = "relevance"
SORT_MODIFIED = "modified"
SORT_FILENAME = "filename"
SORT_MODES = (SORT_RELEVANCE, SORT_MODIFIED, SORT_FILENAME)


def validate_sort_mode(sort_mode: str) -> str:
    """Return a known sort mode or reject invalid internal/UI values."""
    if sort_mode not in SORT_MODES:
        raise ValueError(f"unsupported search sort mode: {sort_mode}")
    return sort_mode


def search_order_clause(sort_mode: str) -> str:
    """Return a fixed SQL ordering for keyword search file hits.

    The result is selected from a closed allow-list rather than interpolating
    user-controlled text into SQL.
    """
    mode = validate_sort_mode(sort_mode)
    if mode == SORT_MODIFIED:
        return "modified_time DESC, relevance_score ASC, path ASC"
    if mode == SORT_FILENAME:
        return "LOWER(filename) ASC, filename ASC, modified_time DESC, path ASC"
    return "relevance_score ASC, modified_time DESC, path ASC"


def browse_order_clause(sort_mode: str) -> str:
    """Return file ordering for metadata-only browsing.

    With no keyword there is no relevance score, so the default relevance mode
    intentionally falls back to the existing recent-modification ordering.
    """
    mode = validate_sort_mode(sort_mode)
    if mode == SORT_FILENAME:
        return "LOWER(filename) ASC, filename ASC, modified_time DESC, path ASC"
    return "modified_time DESC, LOWER(filename) ASC, path ASC"
