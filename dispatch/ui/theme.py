"""Light, quiet Qt stylesheet. Deliberately plain: the data is the interface."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette


def light_palette() -> QPalette:
    """A light palette regardless of the OS dark mode.

    Widgets not covered by the stylesheet (wizard headers, scroll viewports, native
    dialogs) otherwise inherit Windows' dark palette and become unreadable.
    """
    p = QPalette()
    p.setColor(QPalette.Window, QColor("#f5f6f8"))
    p.setColor(QPalette.WindowText, QColor("#1f2933"))
    p.setColor(QPalette.Base, QColor("#ffffff"))
    p.setColor(QPalette.AlternateBase, QColor("#fafbfc"))
    p.setColor(QPalette.ToolTipBase, QColor("#101828"))
    p.setColor(QPalette.ToolTipText, QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, QColor("#98a2b3"))
    p.setColor(QPalette.Text, QColor("#1f2933"))
    p.setColor(QPalette.Button, QColor("#ffffff"))
    p.setColor(QPalette.ButtonText, QColor("#344054"))
    p.setColor(QPalette.BrightText, QColor("#b42318"))
    p.setColor(QPalette.Link, QColor("#1a5fb4"))
    p.setColor(QPalette.Highlight, QColor("#1a5fb4"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#98a2b3"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#98a2b3"))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#98a2b3"))
    return p


QSS = """
* { font-family: "Segoe UI", -apple-system, Roboto, Helvetica, Arial, sans-serif; }
QMainWindow, QDialog { background: #f5f6f8; }
QWidget { color: #1f2933; font-size: 13px; }
QFrame#Card {
    background: #ffffff; border: 1px solid #e3e6ea; border-radius: 10px;
}
QLabel#Title { font-size: 17px; font-weight: 600; color: #101828; }
QLabel#Subtitle { color: #667085; font-size: 12px; }
QLabel#Section { font-weight: 600; color: #344054; font-size: 12px;
                 text-transform: uppercase; letter-spacing: .4px; }
QLabel#Counter { font-size: 22px; font-weight: 600; color: #101828; }
QLabel#Big { font-size: 20px; font-weight: 600; color: #101828; }
QLabel#Muted { color: #667085; }
QLabel#Pill {
    padding: 4px 12px; border-radius: 10px; font-weight: 600; font-size: 12px;
    background: #eef2f6; color: #344054;
}
QLabel#PillRunning { background: #e3f2e8; color: #1c6b3c; }
QLabel#PillPaused { background: #fff2d8; color: #8a5a00; }
QLabel#PillStopped { background: #fde8e8; color: #a02020; }
QLabel#DryBanner {
    background: #fff4d6; color: #7a4b00; border: 1px solid #f2d391;
    border-radius: 8px; padding: 8px 12px; font-weight: 600;
}
QLabel#Error { color: #b42318; }
QLabel#Ok { color: #1c6b3c; }
QPushButton {
    background: #ffffff; border: 1px solid #d0d5dd; border-radius: 6px;
    padding: 6px 14px; color: #344054;
}
QPushButton:hover { background: #f9fafb; }
QPushButton:pressed { background: #eef2f6; }
QPushButton:disabled { color: #98a2b3; background: #f2f4f7; }
QPushButton#Primary {
    background: #1a5fb4; border: 1px solid #1a5fb4; color: #ffffff; font-weight: 600;
}
QPushButton#Primary:hover { background: #17539c; }
QPushButton#Danger { color: #b42318; border-color: #f0c2bd; }
QPushButton#Chip {
    border-radius: 12px; padding: 4px 14px; background: #ffffff; color: #475467;
}
QPushButton#Chip:checked {
    background: #1a5fb4; border-color: #1a5fb4; color: #ffffff; font-weight: 600;
}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit {
    background: #ffffff; border: 1px solid #d0d5dd; border-radius: 6px;
    padding: 5px 8px; selection-background-color: #cfe0f7;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #1a5fb4;
}
QTableView {
    background: #ffffff; border: 1px solid #e3e6ea; border-radius: 8px;
    gridline-color: #f0f2f5; selection-background-color: #e8f0fb;
    selection-color: #101828; alternate-background-color: #fafbfc;
}
QHeaderView::section {
    background: #f7f8fa; color: #475467; padding: 7px 8px; border: none;
    border-bottom: 1px solid #e3e6ea; font-weight: 600;
}
QTableCornerButton::section { background: #f7f8fa; border: none; }
QListWidget {
    background: #ffffff; border: 1px solid #d0d5dd; border-radius: 8px; padding: 4px;
}
QListWidget::item { padding: 7px 8px; border-radius: 5px; }
QListWidget::item:selected { background: #e8f0fb; color: #101828; }
QTextBrowser { background: #ffffff; border: 1px solid #e3e6ea; border-radius: 8px; }
/* Scroll areas must not inherit a dark system palette (Windows dark mode): the
   viewport and the scrolled widget would otherwise paint near-black behind labels. */
QScrollArea { background: #f5f6f8; border: none; }
QScrollArea > QWidget > QWidget { background: #f5f6f8; }
QScrollArea QWidget#qt_scrollarea_viewport { background: #f5f6f8; }
QLabel { background: transparent; }
QWizard, QWizardPage { background: #f5f6f8; }
QGroupBox {
    border: 1px solid #e3e6ea; border-radius: 8px; margin-top: 14px;
    background: #ffffff; padding-top: 10px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #475467;
    font-weight: 600;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #cfd4dc; border-radius: 5px; min-height: 24px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #cfd4dc; border-radius: 5px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: transparent; }
QMenuBar { background: #f5f6f8; }
QMenu { background: #ffffff; border: 1px solid #e3e6ea; }
QStatusBar { color: #667085; }
QToolTip { background: #101828; color: #ffffff; border: none; padding: 5px; }
"""
