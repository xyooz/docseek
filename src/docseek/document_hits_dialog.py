from __future__ import annotations

import html
import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from .document_hits import document_hits
from .location_preview import preview_kind
from .preview_dialog import LocationPreviewDialog
from .structure_store import preview_location


class HitSignals(QObject):
    finished = Signal(object, object)


class HitWorker(QRunnable):
    def __init__(self, store, selected, query, offset):
        super().__init__()
        self.store, self.selected, self.query, self.offset = store, selected, query, offset
        self.cancelled = threading.Event()
        self.signals = HitSignals()

    def run(self):
        try:
            result = document_hits(self.store, self.selected, self.query, offset=self.offset,
                                   cancelled=self.cancelled.is_set)
            self.signals.finished.emit(result, None)
        except Exception as exc:
            self.signals.finished.emit(None, str(exc))


class DocumentHitsDialog(QDialog):
    def __init__(self, store, selected, query, parent=None):
        super().__init__(parent)
        self.store, self.selected, self.query = store, selected, query
        self.offset = 0
        self.closed = False
        self.items = []
        self.setWindowTitle(f"文档内命中 — {selected.filename}")
        self.resize(850, 650)
        self.browser = QTextBrowser(self)
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._open)
        self.previous = QPushButton("上一批", self)
        self.next = QPushButton("下一批", self)
        self.label = QLabel("按文档顺序，每批最多 30 处", self)
        self.previous.clicked.connect(lambda: self._move(-30))
        self.next.clicked.connect(lambda: self._move(30))
        controls = QHBoxLayout()
        controls.addWidget(self.previous)
        controls.addWidget(self.label)
        controls.addWidget(self.next)
        layout = QVBoxLayout(self)
        layout.addWidget(self.browser)
        layout.addLayout(controls)
        self._load()

    def _move(self, delta):
        self.offset = max(0, self.offset + delta)
        self._load()

    def _load(self):
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        self.items = []
        self.browser.setPlainText("正在加载文档内命中…")
        self.worker = HitWorker(self.store, self.selected, self.query, self.offset)
        self.worker.signals.finished.connect(self._finished)
        QThreadPool.globalInstance().start(self.worker)

    def _finished(self, result, error):
        if self.closed:
            return
        if error:
            self.browser.setPlainText(error)
            self.previous.setEnabled(self.offset > 0)
            return
        self.items, more = result
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(more)
        self.label.setText(f"第 {self.offset + 1}-{self.offset + len(self.items)} 处 · 按文档顺序" if self.items else "没有匹配片段")
        sections = []
        for index, item in enumerate(self.items):
            snippet = html.escape(item.snippet).replace("[[HIT]]", "<b style='background-color:#fff2a8'>").replace("[[/HIT]]", "</b>")
            section = f"<h3>{html.escape(item.location)}</h3><p>{snippet}</p>"
            if preview_kind(item.extension, preview_location(item)):
                section += f'<p><a href="hit:{index}">查看原文件位置</a></p>'
            sections.append(section)
        self.browser.setHtml("<hr>".join(sections) or "没有匹配片段，请重新搜索。")

    def _open(self, url):
        if url.scheme() != "hit":
            return
        try:
            index = int(url.path())
        except ValueError:
            return
        if 0 <= index < len(self.items):
            dialog = LocationPreviewDialog(self.items[index], self)
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()

    def done(self, code):
        self.closed = True
        self.worker.cancelled.set()
        super().done(code)
