"""어두운 테마 색상과 Qt 스타일."""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

SURFACE = "#1a1a19"
SURFACE_2 = "#232322"
SURFACE_3 = "#2d2d2b"
GRID = "#2f2f2d"
TRACK = "#3a3a37"
TEXT = "#ffffff"
TEXT_SECONDARY = "#c3c2b7"
TEXT_MUTED = "#8a897f"
ACCENT = "#3987e5"
WIN = "#3987e5"
LOSS = "#e66767"

STYLE = f"""
QWidget {{ background: {SURFACE}; color: {TEXT}; font-size: 13px; }}
QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{ background: {SURFACE}; color: {TEXT_SECONDARY}; padding: 8px 18px; border: 0; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QPushButton, QToolButton {{ background: {SURFACE_3}; border: 1px solid #3c3c39; border-radius: 6px;
    padding: 5px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: #383836; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {SURFACE_2}; border: 1px solid #3c3c39;
    border-radius: 5px; padding: 4px 6px; }}
QTableWidget, QListWidget, QTreeWidget {{ background: {SURFACE_2}; border: 0; gridline-color: {GRID};
    alternate-background-color: #20201f; }}
QHeaderView::section {{ background: {SURFACE}; color: {TEXT_SECONDARY}; border: 0;
    border-bottom: 1px solid {GRID}; padding: 6px; }}
QTableWidget::item:selected, QListWidget::item:selected {{ background: #2b4a70; }}
QGroupBox {{ border: 1px solid {GRID}; border-radius: 8px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: {TEXT_SECONDARY}; }}
QStatusBar {{ color: {TEXT_SECONDARY}; }}
QLabel#muted {{ color: {TEXT_MUTED}; }}
QLabel#tileValue {{ font-size: 22px; font-weight: 600; }}
QLabel#tileLabel {{ color: {TEXT_SECONDARY}; font-size: 12px; }}
QFrame#tile {{ background: {SURFACE_2}; border-radius: 8px; }}
QFrame#tile QLabel {{ background: transparent; }}
QCheckBox {{ spacing: 6px; background: transparent; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid #6e6d66; border-radius: 3px;
    background: {SURFACE_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: {TRACK}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 12px; margin: -5px 0; border-radius: 6px; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(SURFACE))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(SURFACE_2))
    pal.setColor(QPalette.AlternateBase, QColor("#20201f"))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(SURFACE_3))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ToolTipBase, QColor(SURFACE_3))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
