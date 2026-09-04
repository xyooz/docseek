from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .extractors import SUPPORTED_EXTENSIONS, extract_text
from .search_db import SearchDatabase


APP_DIR = Path.home() / ".docseek"
DB_PATH = APP_DIR / "docseek.db"


class IndexSignals(QObject):
    progress = Signal(str)
    finished = Signal(int, int)


class IndexWorker(QRunnable):
    def __init__(self, root: Path, db_path: Path) -> None:
        super().__init__()
        self.root = root
        self.db_path = db_path
        self.signals = IndexSignals()

    def run(self) -> None:
        database = SearchDatabase(self.db_path)
        indexed = 0
        skipped = 0

        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            try:
                self.signals.progress.emit(str(path))
                stat = path.stat()
                content = extract_text(path)
                database.upsert_document(
                    path=str(path),
                    filename=path.name,
                    extension=path.suffix.lower(),
                    modified_time=stat.st_mtime,
                    size=stat.st_size,
                    content=content,
                )
                indexed += 1
            except Exception:
                skipped += 1

        self.signals.finished.emit(indexed, skipped)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DocSeek")
        self.resize(980, 680)

        self.database = SearchDatabase(DB_PATH)
        self.thread_pool = QThreadPool.globalInstance()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索文件名或文件内容，例如：客户经理 信贷")

        self.choose_button = QPushButton("选择目录并建立索引")
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.itemDoubleClicked.connect(self._open_result)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_input, 1)
        top_bar.addWidget(self.choose_button)

        layout = QVBoxLayout()
        layout.addLayout(top_bar)
        layout.addWidget(QLabel("双击搜索结果可使用系统默认程序打开文件"))
        layout.addWidget(self.results, 1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.setStatusBar(QStatusBar())

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self._perform_search)

        self.search_input.textChanged.connect(self.search_timer.start)
        self.choose_button.clicked.connect(self._choose_directory)

    def _choose_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择需要索引的目录")
        if not selected:
            return

        self.choose_button.setEnabled(False)
        worker = IndexWorker(Path(selected), DB_PATH)
        worker.signals.progress.connect(
            lambda path: self.statusBar().showMessage(f"正在索引：{path}")
        )
        worker.signals.finished.connect(self._index_finished)
        self.thread_pool.start(worker)

    def _index_finished(self, indexed: int, skipped: int) -> None:
        self.choose_button.setEnabled(True)
        self.statusBar().showMessage(
            f"索引完成：成功 {indexed} 个，跳过 {skipped} 个",
            10000,
        )
        self._perform_search()

    def _perform_search(self) -> None:
        query = self.search_input.text().strip()
        self.results.clear()
        if not query:
            return

        try:
            rows = self.database.search(query)
        except Exception as exc:
            self.statusBar().showMessage(f"搜索失败：{exc}", 8000)
            return

        for row in rows:
            item = QListWidgetItem(
                f"{row.filename}\n{row.snippet}\n{row.path}"
            )
            item.setData(256, row.path)
            self.results.addItem(item)

        self.statusBar().showMessage(f"找到 {len(rows)} 条结果")

    def _open_result(self, item: QListWidgetItem) -> None:
        path = item.data(256)
        if not path:
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            self.statusBar().showMessage(f"无法打开文件：{exc}", 8000)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
