from __future__ import annotations

from PySide6.QtCore import QByteArray


RESULTS_HEADER_STATE_KEY = "results_header_state_v1"
DEFAULT_COLUMN_WIDTHS = (230, 170, 72, 90, 145, 420)


def encode_header_state(state: QByteArray) -> str:
    """Encode Qt's opaque header state as settings-safe ASCII."""
    return bytes(state.toBase64()).decode("ascii")


def decode_header_state(value: str | None) -> QByteArray | None:
    """Decode a saved header state, rejecting empty or malformed values."""
    if not value:
        return None
    try:
        raw = QByteArray.fromBase64(value.encode("ascii"))
    except (UnicodeEncodeError, ValueError):
        return None
    return raw if not raw.isEmpty() else None
