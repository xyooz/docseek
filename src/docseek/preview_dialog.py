from __future__ import annotations

import base64
import json
import sys

from PySide6.QtCore import QProcess, QTimer, QUrl
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout
from .structure_store import preview_location


class LocationPreviewDialog(QDialog):
    """On-demand isolated preview with timeout and close-time cancellation."""

    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"位置预览 — {result.filename}")
        self.resize(900, 700)
        self.browser = QTextBrowser(self)
        self.browser.setOpenLinks(False)
        self.browser.setPlainText("正在加载位置预览…")
        layout = QVBoxLayout(self)
        layout.addWidget(self.browser)
        self.process = QProcess(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.payload = bytearray()
        self.stopped = False
        self.process.readyReadStandardOutput.connect(self._read)
        args = [result.path, preview_location(result), str(result.modified_time), str(result.size)]
        prefix = ["--docseek-preview-worker"] if getattr(sys, "frozen", False) else ["-m", "docseek.location_preview"]
        self.process.start(sys.executable, prefix + args)
        self.timer.start(15000)

    def _read(self):
        self.payload.extend(bytes(self.process.readAllStandardOutput()))
        if len(self.payload) > 24 * 1024 * 1024:
            self._stop("预览内容过大，请使用原程序打开。")

    def _stop(self, message):
        self.stopped = True
        self.timer.stop()
        self.process.kill()
        self.browser.setPlainText(message)

    def _timeout(self):
        self._stop("预览超过 15 秒，已停止加载。请使用原程序打开。")

    def _error(self, error):
        if not self.stopped:
            self._stop("无法启动或完成预览，请使用原程序打开。")

    def _finished(self, code, status):
        self.timer.stop()
        self._read()
        if self.stopped:
            return
        try:
            result = json.loads(self.payload)
            if "error" in result:
                self.browser.setPlainText(result["error"])
                return
            if "png" in result:
                image = QImage.fromData(base64.b64decode(result["png"]), "PNG")
                self.browser.document().addResource(QTextDocument.ImageResource, QUrl("preview:page"), image)
            self.browser.setHtml(result["html"])
        except Exception:
            self.browser.setPlainText("预览失败，请使用原程序打开。")

    def done(self, code):
        self.stopped = True
        self.timer.stop()
        self.process.kill()
        self.process.waitForFinished(1000)
        super().done(code)
