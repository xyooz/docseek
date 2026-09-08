from __future__ import annotations


# A restrained light theme keeps the application familiar on Windows 10 while
# giving the search workspace clearer hierarchy and larger interaction targets.
APPLICATION_STYLESHEET = """
QMainWindow, QDialog, QWidget#welcomeCanvas { background: #F3F6FB; }
QWidget {
    color: #172033;
    font-family: "Microsoft YaHei UI", "Segoe UI", "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", sans-serif;
    font-size: 13px;
}
QLineEdit#searchInput {
    background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 12px;
    padding: 0 14px; selection-background-color: #2563EB;
}
QLineEdit#searchInput:hover { border-color: #94A3B8; }
QLineEdit#searchInput:focus { border: 2px solid #4F46E5; padding: 0 13px; }
QComboBox {
    background: #FFFFFF; border: 1px solid #C9D3E1; border-radius: 9px;
    padding: 0 28px 0 11px;
}
QComboBox:hover { border-color: #9DACC1; }
QComboBox:focus { border-color: #4F46E5; }
QComboBox::drop-down { border: none; width: 24px; }
QPushButton, QToolButton {
    background: #FFFFFF; border: 1px solid #C9D3E1; border-radius: 8px;
    padding: 0 12px; color: #344054;
}
QPushButton:hover, QToolButton:hover {
    background: #F8FAFC; border-color: #98A8BE;
}
QPushButton:pressed, QToolButton:pressed { background: #EEF2F7; }
QPushButton:disabled, QToolButton:disabled {
    color: #98A2B3; background: #F2F4F7; border-color: #E4E7EC;
}
QPushButton[primary="true"] {
    color: #FFFFFF; background: #4F46E5; border-color: #4F46E5; font-weight: 600;
}
QPushButton[primary="true"]:hover { background: #4338CA; border-color: #4338CA; }
QPushButton[primary="true"]:pressed { background: #3730A3; }
QPushButton[quiet="true"] {
    background: transparent; border-color: transparent; color: #4F46E5;
}
QPushButton[quiet="true"]:hover { background: #EFF6FF; }
QPushButton[danger="true"] {
    color: #B42318; background: #FFF7F6; border-color: #FECDCA;
}
QPushButton[danger="true"]:hover { background: #FEE4E2; border-color: #FDA29B; }
QFrame#workspaceBar, QWidget#indexProgressPanel {
    background: #FFFFFF; border: 1px solid #DFE5EE; border-radius: 10px;
}
QFrame#surfacePanel {
    background: #FFFFFF; border: 1px solid #DFE5EE; border-radius: 12px;
}
QFrame#welcomeCard {
    background: #FFFFFF; border: 1px solid #DFE5EE; border-radius: 16px;
}
QFrame#welcomeInfo {
    background: #F8FAFC; border: 1px solid #E4EAF2; border-radius: 10px;
}
QLabel#eyebrowLabel { color: #4F46E5; font-size: 12px; font-weight: 700; }
QLabel#welcomeTitle { color: #101828; font-size: 27px; font-weight: 700; }
QLabel#welcomeDescription { color: #475467; font-size: 14px; }
QLabel#sectionTitle { color: #101828; font-size: 14px; font-weight: 700; }
QLabel#sectionMeta, QLabel#mutedLabel, QLabel#scopeLabel { color: #667085; }
QTableView#resultsTable, QTextBrowser#previewPane {
    background: #FFFFFF; border: none; gridline-color: #EDF0F5;
    selection-background-color: #EEF2FF; selection-color: #172033;
}
QTableView#resultsTable { alternate-background-color: #FAFBFC; }
QHeaderView::section {
    background: #F8FAFC; color: #475467; border: none;
    border-bottom: 1px solid #E4E7EC; padding: 9px 8px; font-weight: 600;
}
QSplitter::handle { background: #F5F7FB; }
QSplitter::handle:hover { background: #DCE6F5; }
QProgressBar { background: #E7ECF3; border: none; border-radius: 4px; }
QProgressBar::chunk { background: #4F46E5; border-radius: 4px; }
QStatusBar {
    background: #F5F7FB; color: #667085; border-top: 1px solid #E4E7EC;
}
QMenu { background: #FFFFFF; border: 1px solid #DDE3EC; padding: 6px; }
QMenu::item { border-radius: 6px; padding: 7px 24px 7px 10px; }
QMenu::item:selected { background: #EEF2FF; color: #4338CA; }
QTabWidget::pane {
    background: #FFFFFF; border: 1px solid #DFE5EE; border-radius: 10px;
}
QTabBar::tab {
    background: transparent; color: #667085; padding: 9px 18px;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected { color: #4338CA; border-bottom-color: #4F46E5; font-weight: 600; }
QTabBar::tab:hover { color: #344054; background: #F8FAFC; }
QGroupBox {
    background: #FFFFFF; border: 1px solid #E4E7EC; border-radius: 9px;
    margin-top: 10px; padding-top: 8px; font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: #CBD5E1; border-radius: 4px; min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: #94A3B8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCheckBox { spacing: 8px; padding: 3px 0; }
QCheckBox::indicator { width: 16px; height: 16px; }
QToolTip {
    color: #F8FAFC; background: #1E293B; border: none;
    border-radius: 6px; padding: 6px 8px;
}
"""
