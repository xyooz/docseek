from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from .chunk_store import ChunkSearchResult


class SearchResultsModel(QAbstractTableModel):
    """Table model for incremental file-level search results.

    Keeping result objects in a model avoids creating and retaining six
    QTableWidgetItem objects per row. This is especially useful as the desktop
    view incrementally loads hundreds or thousands of matching files.
    """

    HEADERS = ("文件名", "命中位置", "类型", "大小", "修改时间", "路径")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[ChunkSearchResult] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            if 0 <= section < len(self.HEADERS):
                return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        row = self._items[index.row()]

        if role == Qt.UserRole:
            return row.path
        if role == Qt.ToolTipRole:
            if index.column() == 0:
                return row.path
            if index.column() == 1 and row.location:
                return row.location
            return None
        if role != Qt.DisplayRole:
            return None

        column = index.column()
        if column == 0:
            return row.filename
        if column == 1:
            return row.location or "—"
        if column == 2:
            return row.extension.lstrip(".").upper()
        if column == 3:
            return self.human_size(row.size)
        if column == 4:
            return datetime.fromtimestamp(row.modified_time).strftime("%Y-%m-%d %H:%M")
        if column == 5:
            return row.path
        return None

    def clear(self) -> None:
        if not self._items:
            return
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()

    def append_items(self, items: list[ChunkSearchResult]) -> None:
        if not items:
            return
        first = len(self._items)
        last = first + len(items) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        self._items.extend(items)
        self.endInsertRows()

    def result_at(self, row: int) -> ChunkSearchResult | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    @staticmethod
    def human_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"
