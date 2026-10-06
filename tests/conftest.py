import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# offscreen 에서도 실제 글꼴로 크기를 재도록 (Windows)
if os.name == "nt" and os.path.isdir(r"C:\Windows\Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
