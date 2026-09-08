from __future__ import annotations

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QIcon, QPixmap


_PATHS = {
    "search": '<circle cx="10.5" cy="10.5" r="5.75"/><path d="m15 15 4.25 4.25"/>',
    "history": '<path d="M4.5 8.2A7.5 7.5 0 1 1 4.8 16"/><path d="M4.5 3.8v4.7h4.7M12 7.5V12l3 1.8"/>',
    "folder-plus": '<path d="M3 6.5h6l1.8 2H21v9.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v6m-3-3h6"/>',
    "settings": '<path d="M4 6h5m4 0h7M9 3.5v5M4 12h10m4 0h2m-6-2.5v5M4 18h2m4 0h10M6 15.5v5"/>',
    "more": '<circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none"/>',
    "refresh": '<path d="M19 8a7.5 7.5 0 0 0-13-2L3.5 8.5M5 16a7.5 7.5 0 0 0 13-2l2.5-2.5"/><path d="M3.5 4v4.5H8M20.5 16v-4.5H16"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.8 9a2.4 2.4 0 1 1 3.5 2.15c-.9.45-1.3 1-1.3 1.85M12 17h.01"/>',
}


def line_icon(name: str, *, color: str = "#475569") -> QIcon:
    """Return a small, dependency-free SVG line icon for desktop controls."""
    body = _PATHS.get(name)
    if body is None:
        return QIcon()
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"
        viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round" style="color:{color}">
        {body}</svg>'''
    pixmap = QPixmap()
    pixmap.loadFromData(QByteArray(svg.encode("utf-8")), "SVG")
    return QIcon(pixmap)
