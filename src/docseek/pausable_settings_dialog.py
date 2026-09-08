from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .diagnostics import write_diagnostic_report
from .file_exclusions import (
    FileExclusionStore,
    InvalidFileExclusionPattern,
    normalize_file_exclusion_patterns,
)
from .index_health import (
    capture_index_health,
    format_index_health,
    format_reconcile_time,
)
from .index_issues_dialog import IndexIssuesDialog
from .index_maintenance import (
    ACTION_BACKUP,
    ACTION_REBUILD,
    ACTION_RESET,
    ACTION_STAGE_RESTORE,
    MaintenanceResult,
    perform_maintenance,
)
from .index_retry import build_index_retry_plan, dispatch_index_retry
from .index_root_state import IndexRootStateStore
from .retryable_index_issues_dialog import RetryableIndexIssuesDialog
from .settings_dialog import IndexSettingsDialog


class MaintenanceSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class MaintenanceWorker(QRunnable):
    def __init__(self, *, action: str, db_path: Path, path: str | Path | None = None) -> None:
        super().__init__()
        self.action = action
        self.db_path = Path(db_path)
        self.path = path
        self.signals = MaintenanceSignals()

    def run(self) -> None:
        try:
            result = perform_maintenance(
                self.action,
                self.db_path,
                path=self.path,
            )
        except Exception as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.finished.emit(result)


class PausableIndexSettingsDialog(IndexSettingsDialog):
    """Index settings with pause/resume, health, retry and safe maintenance."""

    def __init__(self, database, parent=None) -> None:
        super().__init__(database, parent)
        self.resize(1060, 700)
        self.setMinimumSize(900, 620)
        self.root_state_store = IndexRootStateStore(database)
        self.file_exclusion_store = FileExclusionStore(database)
        self._maintenance_worker: MaintenanceWorker | None = None
        self._watcher_stopped_for_maintenance = False

        note = QLabel(
            "勾选 = 正常监测和刷新；取消勾选 = 暂停更新，但保留现有索引和搜索结果。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748B;")
        self.layout().insertWidget(0, note)
        self.settings_note = note

        paused = set(self.root_state_store.paused_roots())
        self._original_paused = paused
        for row in range(self.root_list.count()):
            item = self.root_list.item(row)
            self._make_checkable(item, checked=self._item_path(item) not in paused)

        self.root_list.setToolTip(
            "取消勾选并保存后，暂停该目录的自动监测和手动刷新，已有内容仍可搜索。"
            "重新勾选并保存后恢复更新。"
        )

        patterns = self.file_exclusion_store.patterns()
        self._original_file_pattern_text = "\n".join(patterns)
        self.file_pattern_edit = QPlainTextEdit()
        self.file_pattern_edit.setPlainText(self._original_file_pattern_text)
        self.file_pattern_edit.setPlaceholderText("每行一条，例如：*.log   *.bak   测试_*   .tmp")
        self.file_pattern_edit.setMaximumHeight(92)
        self.file_pattern_edit.setToolTip(
            "只匹配文件名，大小写不敏感；.log 会自动按 *.log 处理。"
        )

        pattern_hint = QLabel(
            "每行一条文件名通配规则，例如 *.log、*.bak、测试_*。"
            "只匹配文件名，不支持路径或正则；保存后会校准活动目录并清理已有匹配索引。"
        )
        pattern_hint.setWordWrap(True)
        pattern_hint.setStyleSheet("color: #64748B; font-size: 12px;")

        pattern_group = QGroupBox("排除文件规则")
        pattern_layout = QVBoxLayout()
        pattern_layout.addWidget(self.file_pattern_edit)
        pattern_layout.addWidget(pattern_hint)
        pattern_group.setLayout(pattern_layout)
        self.pattern_group = pattern_group

        self.health_summary_label = QLabel()
        self.health_summary_label.setWordWrap(True)
        self.health_summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reconcile_time_label = QLabel()
        self.reconcile_time_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reconcile_time_label.setStyleSheet("color: #64748B;")
        self.health_hint_label = QLabel(
            "占用包含 SQLite 主库及当前 WAL/SHM 文件；暂停目录的现有索引仍计入文件数和空间占用。"
        )
        self.health_hint_label.setWordWrap(True)
        self.health_hint_label.setStyleSheet("color: #64748B; font-size: 12px;")
        self.diagnostic_button = QPushButton("导出脱敏诊断…")
        self.diagnostic_button.setToolTip(
            "导出版本、运行环境和索引健康统计；不包含目录路径、文件名、正文、问题详情或搜索记录。"
        )
        self.diagnostic_button.clicked.connect(self._export_diagnostics)
        self.diagnostic_hint_label = QLabel(
            "诊断文件只包含聚合状态，不包含索引目录路径、问题文件路径、文件名、正文或搜索记录。"
        )
        self.diagnostic_hint_label.setWordWrap(True)
        self.diagnostic_hint_label.setStyleSheet("color: #64748B; font-size: 12px;")

        health_group = QGroupBox("索引概况")
        health_layout = QVBoxLayout()
        health_layout.addWidget(self.health_summary_label)
        health_layout.addWidget(self.reconcile_time_label)
        health_layout.addWidget(self.health_hint_label)
        health_layout.addWidget(self.diagnostic_button)
        health_layout.addWidget(self.diagnostic_hint_label)
        health_group.setLayout(health_layout)
        self.health_group = health_group

        self.backup_button = QPushButton("备份索引…")
        self.rebuild_button = QPushButton("重建索引…")
        self.reset_button = QPushButton("清空索引…")
        self.restore_button = QPushButton("恢复备份…")
        self.backup_button.clicked.connect(self._request_backup)
        self.rebuild_button.clicked.connect(self._request_rebuild)
        self.reset_button.clicked.connect(self._request_reset)
        self.restore_button.clicked.connect(self._request_restore)

        maintenance_buttons = QHBoxLayout()
        maintenance_buttons.addWidget(self.backup_button)
        maintenance_buttons.addWidget(self.rebuild_button)
        maintenance_buttons.addWidget(self.reset_button)
        maintenance_buttons.addWidget(self.restore_button)
        maintenance_buttons.addStretch(1)

        self.maintenance_status_label = QLabel("")
        self.maintenance_status_label.setWordWrap(True)
        self.maintenance_status_label.setStyleSheet("color: #64748B;")
        maintenance_hint = QLabel(
            "备份包含完整索引正文，应按敏感文件保护。重建会先自动备份再重新建立索引；"
            "清空会先自动备份，再删除本地索引和索引范围配置，但绝不会删除源文件。"
            "恢复采用下次启动前替换，避免运行中覆盖正在使用的数据库。"
        )
        maintenance_hint.setWordWrap(True)
        maintenance_hint.setStyleSheet("color: #64748B; font-size: 12px;")

        maintenance_group = QGroupBox("维护与恢复")
        maintenance_layout = QVBoxLayout()
        maintenance_layout.addLayout(maintenance_buttons)
        maintenance_layout.addWidget(self.maintenance_status_label)
        maintenance_layout.addWidget(maintenance_hint)
        maintenance_group.setLayout(maintenance_layout)
        self.maintenance_group = maintenance_group

        self._install_tabbed_layout()
        self._root_indexed_count_cache: dict[str, int] = {}
        self._refresh_root_progress_labels()
        self._refresh_health_summary()

        self.indexing_read_only = bool(
            parent is not None and getattr(parent, "current_worker", None) is not None
        )
        self.progress_refresh_timer = QTimer(self)
        self.progress_refresh_timer.setInterval(300)
        self.progress_refresh_timer.timeout.connect(self._refresh_live_progress)
        if self.indexing_read_only:
            self._set_indexing_read_only(True)
            self.progress_refresh_timer.start()

    def _install_tabbed_layout(self) -> None:
        """Use the available width instead of growing one long settings page."""
        main_layout = self.layout()
        groups = (
            self.roots_group,
            self.exclude_group,
            self.format_group,
            self.advanced_group,
            self.storage_group,
            self.issues_group,
        )
        for group in groups:
            main_layout.removeWidget(group)

        directory_page = QWidget()
        directory_layout = QHBoxLayout(directory_page)
        directory_layout.setContentsMargins(10, 12, 10, 10)
        directory_layout.setSpacing(12)
        directory_layout.addWidget(self.roots_group, 3)
        directory_layout.addWidget(self.exclude_group, 2)

        rules_page = QWidget()
        rules_layout = QHBoxLayout(rules_page)
        rules_layout.setContentsMargins(10, 12, 10, 10)
        rules_layout.setSpacing(12)
        rules_left = QVBoxLayout()
        rules_left.addWidget(self.format_group)
        rules_left.addWidget(self.pattern_group)
        rules_left.addWidget(self.advanced_group)
        rules_left.addStretch(1)
        rules_layout.addLayout(rules_left, 1)

        status_page = QWidget()
        status_layout = QVBoxLayout(status_page)
        status_layout.setContentsMargins(10, 12, 10, 10)
        status_layout.setSpacing(10)
        status_layout.addWidget(self.storage_group)
        status_layout.addWidget(self.health_group)
        status_layout.addWidget(self.issues_group)
        status_layout.addWidget(self.maintenance_group)
        status_layout.addStretch(1)

        self.settings_tabs = QTabWidget()
        self.settings_tabs.addTab(directory_page, "索引范围")
        self.settings_tabs.addTab(rules_page, "文件规则")
        self.settings_tabs.addTab(status_page, "存储与维护")
        main_layout.insertWidget(1, self.settings_tabs, 1)

    def _indexed_count_under_root(self, root: str) -> int:
        cached = self._root_indexed_count_cache.get(root)
        if cached is not None:
            return cached
        prefix = str(Path(root)).rstrip("\\/") + os.sep
        with self.database.connect() as conn:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM files WHERE path >= ? AND path < ?",
                    (prefix, prefix + "\U0010ffff"),
                ).fetchone()[0]
            )
        self._root_indexed_count_cache[root] = count
        return count

    def _refresh_root_progress_labels(self) -> None:
        parent = self.parentWidget()
        progress_by_root = getattr(parent, "index_root_progress", {})
        paused = set(self.root_state_store.paused_roots())
        for row in range(self.root_list.count()):
            item = self.root_list.item(row)
            root = self._item_path(item)
            progress = progress_by_root.get(root)
            if root in paused:
                status = "已暂停更新"
            elif progress and progress[1] > 0:
                completed, total = progress
                percent = min(100, round(completed * 100 / total))
                status = f"索引进度 {percent}%（{completed:,}/{total:,}）"
            else:
                indexed = self._indexed_count_under_root(root)
                status = f"已索引 {indexed:,} 个文件" if indexed else "等待建立索引"
            item.setText(f"{status}    ·    {root}")

    def _set_indexing_read_only(self, active: bool) -> None:
        self.indexing_read_only = active
        for widget in (
            self.root_list,
            self.exclude_list,
            self.add_root_button,
            self.remove_root_button,
            self.add_exclude_button,
            self.remove_exclude_button,
            self.file_pattern_edit,
            *self.format_checkboxes.values(),
            self.recommended_formats_button,
            self.office_formats_button,
            self.all_formats_button,
            self.clear_formats_button,
            self.format_preset_combo,
            self.max_size,
            self.storage_move_button,
            self.backup_button,
            self.rebuild_button,
            self.reset_button,
            self.restore_button,
        ):
            widget.setEnabled(not active)
        save_button = self.button_box.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setEnabled(not active)
        if active:
            self.settings_note.setText(
                "索引正在运行：此窗口暂时只读，目录后的进度会自动更新；"
                "已完成的内容仍可搜索。"
            )
        else:
            self.settings_note.setText(
                "勾选 = 正常监测和刷新；取消勾选 = 暂停更新，但保留现有索引和搜索结果。"
            )

    def _refresh_live_progress(self) -> None:
        self._refresh_root_progress_labels()
        parent = self.parentWidget()
        if parent is None or getattr(parent, "current_worker", None) is None:
            self.progress_refresh_timer.stop()
            self._root_indexed_count_cache.clear()
            self._set_indexing_read_only(False)
            self._refresh_root_progress_labels()

    @staticmethod
    def _make_checkable(item, *, checked: bool) -> None:
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )

    def _add_root(self) -> None:
        previous_count = self.root_list.count()
        super()._add_root()
        for row in range(previous_count, self.root_list.count()):
            self._make_checkable(self.root_list.item(row), checked=True)
        self._refresh_root_progress_labels()

    def _paused_roots_from_ui(self) -> list[str]:
        return [
            self._item_path(self.root_list.item(row))
            for row in range(self.root_list.count())
            if self.root_list.item(row).checkState() == Qt.CheckState.Unchecked
        ]

    def _file_patterns_from_ui(self) -> list[str]:
        return normalize_file_exclusion_patterns(
            self.file_pattern_edit.toPlainText().splitlines()
        )

    def _has_unsaved_changes(self) -> bool:
        return (
            set(self._items(self.root_list)) != self._original_roots
            or set(self._items(self.exclude_list)) != self._original_excluded
            or set(self._paused_roots_from_ui()) != self._original_paused
            or self.file_pattern_edit.toPlainText().strip()
            != self._original_file_pattern_text.strip()
            or self._enabled_extensions_from_ui()
            != self._original_enabled_extensions
            or self.max_size.value() != self.database.get_max_file_size_mb()
        )

    def _refresh_health_summary(self) -> None:
        if not hasattr(self, "health_summary_label"):
            return
        snapshot = capture_index_health(self.database)
        self.health_summary_label.setText(format_index_health(snapshot))
        if hasattr(self, "reconcile_time_label"):
            self.reconcile_time_label.setText(
                "最近完整校准："
                + format_reconcile_time(snapshot.last_successful_reconcile_at)
            )

    def _export_diagnostics(self) -> None:
        default_name = (
            "docseek-diagnostics-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
        )
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出脱敏诊断",
            str(Path.home() / default_name),
            "JSON 文件 (*.json)",
        )
        if not filename:
            return
        if not filename.lower().endswith(".json"):
            filename += ".json"
        try:
            destination = write_diagnostic_report(self.database, filename)
        except Exception as exc:  # UI boundary: report failure instead of closing settings.
            QMessageBox.warning(self, "导出诊断失败", str(exc))
            return
        QMessageBox.information(
            self,
            "诊断已导出",
            "已保存脱敏诊断：\n"
            f"{destination}\n\n"
            "文件不包含目录路径、文件名、正文、问题详情或搜索记录。",
        )

    def _require_saved_settings(self) -> bool:
        if not self._has_unsaved_changes():
            return True
        QMessageBox.information(
            self,
            "请先保存设置",
            "当前索引设置还有未保存修改。请先点击“保存”，完成正常校准后再执行备份、重建、清空或恢复。",
        )
        return False

    def _stop_parent_watcher_for_destructive_maintenance(self) -> bool:
        parent = self.parentWidget()
        if parent is None:
            return True
        if getattr(parent, "current_worker", None) is not None:
            QMessageBox.information(
                self,
                "索引任务仍在运行",
                "检测到新的索引任务已经开始。请等待任务完成后再执行重建或清空。",
            )
            return False
        watch_manager = getattr(parent, "watch_manager", None)
        if watch_manager is not None:
            watch_manager.stop()
            self._watcher_stopped_for_maintenance = True
        pending = getattr(parent, "pending_watch_paths", None)
        if pending is not None:
            pending.clear()
        if hasattr(parent, "watch_full_rescan_pending"):
            parent.watch_full_rescan_pending = False
        return True

    def _restart_parent_watcher_if_needed(self) -> None:
        if not self._watcher_stopped_for_maintenance:
            return
        parent = self.parentWidget()
        if parent is not None:
            restart = getattr(parent, "_restart_watcher", None)
            if callable(restart):
                restart()
        self._watcher_stopped_for_maintenance = False

    def _start_maintenance(
        self,
        action: str,
        *,
        path: str | Path | None = None,
        status: str,
    ) -> None:
        if self._maintenance_worker is not None:
            return
        worker = MaintenanceWorker(action=action, db_path=self.database.db_path, path=path)
        worker.signals.finished.connect(self._maintenance_finished)
        worker.signals.failed.connect(self._maintenance_failed)
        self._maintenance_worker = worker
        self.maintenance_status_label.setText(status)
        self.setEnabled(False)
        QThreadPool.globalInstance().start(worker)

    def _request_backup(self) -> None:
        if not self._require_saved_settings():
            return
        default_name = "docseek-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "备份 DocSeek 索引",
            str(Path.home() / default_name),
            "DocSeek 索引备份 (*.db)",
        )
        if not filename:
            return
        if not filename.lower().endswith(".db"):
            filename += ".db"
        self._start_maintenance(
            ACTION_BACKUP,
            path=filename,
            status="正在创建一致性索引备份…",
        )

    def _request_rebuild(self) -> None:
        if not self._require_saved_settings():
            return
        if not self.database.get_index_roots():
            QMessageBox.information(self, "没有索引目录", "请先添加并保存至少一个索引目录。")
            return
        answer = QMessageBox.question(
            self,
            "重建索引",
            "DocSeek 将先自动备份当前索引，再清空本地索引内容并重新扫描已启用目录。\n\n"
            "源文件不会被修改或删除。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not self._stop_parent_watcher_for_destructive_maintenance():
            return
        self._start_maintenance(
            ACTION_REBUILD,
            status="正在备份并准备重建索引…",
        )

    def _request_reset(self) -> None:
        if not self._require_saved_settings():
            return
        answer = QMessageBox.question(
            self,
            "清空本地索引",
            "此操作会先自动备份，然后清空 DocSeek 的本地索引、索引目录、排除目录和暂停状态。\n\n"
            "搜索历史、收藏和界面偏好会保留；任何源文件和源目录都不会被删除。\n\n"
            "是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not self._stop_parent_watcher_for_destructive_maintenance():
            return
        self._start_maintenance(
            ACTION_RESET,
            status="正在备份并清空本地索引…",
        )

    def _request_restore(self) -> None:
        if not self._require_saved_settings():
            return
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择 DocSeek 索引备份",
            str(Path.home()),
            "DocSeek 索引备份 (*.db);;所有文件 (*)",
        )
        if not filename:
            return
        answer = QMessageBox.question(
            self,
            "恢复索引备份",
            "所选备份会先校验并暂存，当前运行中的索引不会立即改变。\n"
            "下次启动 DocSeek 时才会替换数据库，启动前还会自动保留当前索引。\n\n"
            "恢复的是完整索引数据库，因此会恢复备份时的索引目录、排除规则、搜索历史和收藏等状态。\n\n"
            "是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._start_maintenance(
            ACTION_STAGE_RESTORE,
            path=filename,
            status="正在校验并暂存索引恢复…",
        )

    def _invalidate_parent_results(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        search_timer = getattr(parent, "search_timer", None)
        if search_timer is not None:
            search_timer.stop()
        if hasattr(parent, "search_generation"):
            parent.search_generation += 1
        model = getattr(parent, "results_model", None)
        if model is not None:
            model.clear()
        preview = getattr(parent, "preview", None)
        if preview is not None:
            preview.clear()

    def _maintenance_finished(self, result: MaintenanceResult) -> None:
        self.setEnabled(True)
        self._maintenance_worker = None
        self.maintenance_status_label.setText("")
        self._refresh_issue_summary()

        if result.action == ACTION_BACKUP:
            QMessageBox.information(
                self,
                "索引备份完成",
                "已创建完整索引备份：\n"
                f"{result.backup_path}\n\n"
                "备份中包含提取后的文档正文，请按敏感文件妥善保存。",
            )
            return

        if result.action == ACTION_STAGE_RESTORE:
            QMessageBox.information(
                self,
                "恢复已暂存",
                "备份已通过校验并暂存。当前索引没有被覆盖。\n\n"
                "请正常关闭并重新启动 DocSeek；下次启动会在打开数据库前应用恢复，并先保存当前索引。",
            )
            return

        self._invalidate_parent_results()
        # Existing MainWindow settings flow will restart the watcher after this
        # dialog closes. Do not restart it in the small gap before a rebuild.
        self._watcher_stopped_for_maintenance = False
        if result.action == ACTION_REBUILD:
            QMessageBox.information(
                self,
                "准备重建索引",
                f"已安全清空 {result.cleared_files:,} 个文件的旧索引。\n"
                f"自动备份：{result.backup_path}\n\n"
                "关闭此窗口后 DocSeek 会立即重新扫描已启用目录。源文件未被修改。",
            )
            # Bypass settings mutation: configuration was intentionally kept.
            QDialog.accept(self)
            return

        if result.action == ACTION_RESET:
            QMessageBox.information(
                self,
                "本地索引已清空",
                f"已清空 {result.cleared_files:,} 个文件的本地索引并移除索引范围配置。\n"
                f"自动备份：{result.backup_path}\n\n"
                "源文件和源目录均未删除。",
            )
            QDialog.accept(self)

    def _maintenance_failed(self, message: str) -> None:
        self.setEnabled(True)
        self._maintenance_worker = None
        self._restart_parent_watcher_if_needed()
        self.maintenance_status_label.setText("维护操作失败；当前索引未按计划完成变更。")
        QMessageBox.warning(self, "索引维护失败", message)

    def reject(self) -> None:
        if self._maintenance_worker is not None:
            QMessageBox.information(self, "维护进行中", "请等待当前维护操作完成。")
            return
        self._restart_parent_watcher_if_needed()
        super().reject()

    def _refresh_issue_summary(self) -> None:
        # Base __init__ calls this virtual method before the health widget exists,
        # so the health refresh is deliberately guarded above.
        super()._refresh_issue_summary()
        self._refresh_health_summary()

    def _show_index_issues(self) -> None:
        # Retry must use the settings the indexer will actually read. If this
        # dialog contains unsaved edits (for example a larger file-size limit),
        # asking the old configuration to retry would be misleading. Keep the
        # viewer available but require Save before exposing retry actions.
        if self._has_unsaved_changes():
            dialog = IndexIssuesDialog(self.issue_store, self)
            dialog.setWindowTitle("索引问题（保存设置后可重试）")
            hint = QLabel(
                "当前索引设置有未保存修改。请先保存设置，让 DocSeek 按新配置校准索引；"
                "之后仍存在的问题可再使用一键重试。"
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #64748B;")
            dialog.layout().insertWidget(1, hint)
            dialog.exec()
            self._refresh_issue_summary()
            return

        dialog = RetryableIndexIssuesDialog(self.issue_store, self)
        if not dialog.exec() or not dialog.retry_paths:
            self._refresh_issue_summary()
            return

        wanted = set(dialog.retry_paths)
        issues = [
            issue
            for issue in self.issue_store.list(limit=1000)
            if issue.path in wanted
        ]
        plan = build_index_retry_plan(
            issues,
            active_roots=self.root_state_store.active_roots(),
        )
        parent = self.parentWidget()

        # Close the settings modal before the main window launches workers.
        # There are no unsaved edits at this point, so nothing is discarded.
        self.reject()
        if parent is not None:
            QTimer.singleShot(
                0,
                lambda target=parent, retry_plan=plan: dispatch_index_retry(
                    target, retry_plan
                ),
            )

    def accept(self) -> None:
        if self._maintenance_worker is not None:
            return
        if not self._validate_enabled_extensions():
            return
        try:
            file_patterns = self._file_patterns_from_ui()
        except InvalidFileExclusionPattern as exc:
            QMessageBox.warning(self, "排除规则无效", str(exc))
            return

        # Validate the file rules before mutating any persisted settings. This
        # keeps Save atomic from the user's point of view when a rule is invalid.
        self.file_exclusion_store.set_patterns(file_patterns)

        # Write the complete intended root-state set before the base dialog
        # applies root additions/removals. paused_roots() always filters against
        # the roots that actually exist, so a failed root mutation cannot make
        # an unrelated directory appear paused.
        roots = self._items(self.root_list)
        self.root_state_store.replace_paused(
            self._paused_roots_from_ui(),
            known_roots=roots,
        )
        super().accept()
