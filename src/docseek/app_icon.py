from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon


def app_icon_path() -> Path:
    """Return the source or PyInstaller-bundled application icon path."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "assets" / "docseek.svg"
    return Path(__file__).resolve().parents[2] / "assets" / "docseek.svg"


def load_app_icon() -> QIcon:
    path = app_icon_path()
    return QIcon(str(path)) if path.exists() else QIcon()
