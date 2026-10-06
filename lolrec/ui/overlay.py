"""죽었을 때 게임 위에 뜨는 즉시 리플레이 창.

  * 항상 위, 테두리 없음, 클릭 통과, 포커스를 가져가지 않음 -> 게임 조작을 방해하지 않음
  * 화면 캡처에서 제외(WDA_EXCLUDEFROMCAPTURE) -> 리플레이 창이 녹화 영상에 찍히지 않음
  * 테두리 없는 창 모드에서 보인다. 전체 화면(독점) 모드에서는 게임이 화면을 독점해서 보이지 않는다.
"""

from __future__ import annotations

import ctypes
import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..config import Settings

WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080


class ReplayOverlay(QWidget):
    def __init__(self, settings: Settings):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.settings = settings
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("background: black; border: 2px solid #e66767;")

        self.label = QLabel("데스 리플레이")
        self.label.setStyleSheet("color: white; background: #b03a3a; padding: 3px 8px; border: 0;"
                                 " font-weight: 600;")
        self.video = QVideoWidget(self)
        self.video.setStyleSheet("border: 0;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        layout.addWidget(self.label)
        layout.addWidget(self.video, 1)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(self.finish)
        self._pending_seek: float | None = None
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self._win_styled = False

    # ------------------------------------------------------------------ public
    def play_clip(self, path: str, seek: float, length: float) -> None:
        self._place()
        self.audio.setVolume(self.settings.replay_volume)
        self._pending_seek = seek
        self.player.setSource(QUrl.fromLocalFile(path))
        self.show()
        self._apply_win_styles()
        self.player.play()
        self._stop_timer.start(int((length + 0.5) * 1000))

    def finish(self) -> None:
        self._stop_timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())
        self.hide()

    # ------------------------------------------------------------------ internals
    def _on_media_status(self, status) -> None:
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self._pending_seek is not None:
            self.player.setPosition(int(self._pending_seek * 1000))
            self._pending_seek = None
        elif status == QMediaPlayer.EndOfMedia:
            self.finish()

    def _place(self) -> None:
        screens = QGuiApplication.screens()
        idx = self.settings.monitor_index
        screen = screens[idx] if 0 <= idx < len(screens) else QGuiApplication.primaryScreen()
        geo = screen.geometry()
        w = int(geo.width() * self.settings.replay_overlay_scale)
        h = int(w * 9 / 16) + self.label.sizeHint().height()
        margin = int(geo.height() * 0.06)
        pos = self.settings.replay_overlay_position
        x = {"left": geo.left() + margin, "right": geo.right() - w - margin}.get(
            pos.split("-")[-1], geo.left() + (geo.width() - w) // 2)
        y = geo.top() + margin if pos.startswith("top") else geo.bottom() - h - margin * 3
        self.setGeometry(x, y, w, h)

    def _apply_win_styles(self) -> None:
        if os.name != "nt" or self._win_styled:
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
            self._win_styled = True
        except Exception:
            pass
