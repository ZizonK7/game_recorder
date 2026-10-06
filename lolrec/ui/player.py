"""녹화 재생 화면: 영상 + 이벤트 재생바 + 이벤트 목록."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QSlider, QSplitter, QVBoxLayout, QWidget,
)

from ..events import DEFAULT_VISIBLE, EVENT_TYPES, GameEvent, load_events
from .timeline import TimelineBar, fmt_time


def _dot_icon(color: str) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(1, 1, 10, 10, 3, 3)
    p.end()
    return QIcon(pm)


class PlayerView(QWidget):
    status = Signal(str)

    def __init__(self, seek_lead: float = 5.0, parent=None):
        super().__init__(parent)
        self.seek_lead = seek_lead
        self.events: list[GameEvent] = []
        self.visible: set[str] = set(DEFAULT_VISIBLE)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget(self)
        self.video.setMinimumSize(480, 270)
        self.video.setStyleSheet("background: black;")
        self.player.setVideoOutput(self.video)

        self.title = QLabel("녹화를 선택하세요")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")

        self.timeline = TimelineBar(self)
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.event_clicked.connect(self.jump_to_event)

        # 컨트롤
        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedWidth(44)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_prev = QPushButton("◀ 이전 이벤트")
        self.btn_prev.clicked.connect(lambda: self.step_event(-1))
        self.btn_next = QPushButton("다음 이벤트 ▶")
        self.btn_next.clicked.connect(lambda: self.step_event(1))
        self.btn_back = QPushButton("-5초")
        self.btn_back.clicked.connect(lambda: self.seek(self.current() - 5))
        self.btn_fwd = QPushButton("+5초")
        self.btn_fwd.clicked.connect(lambda: self.seek(self.current() + 5))
        self.speed = QComboBox()
        for s in ("0.25x", "0.5x", "1x", "1.5x", "2x", "4x"):
            self.speed.addItem(s)
        self.speed.setCurrentText("1x")
        self.speed.currentTextChanged.connect(lambda t: self.player.setPlaybackRate(float(t[:-1])))
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("muted")

        controls = QHBoxLayout()
        for w in (self.btn_play, self.btn_back, self.btn_fwd, self.btn_prev, self.btn_next):
            controls.addWidget(w)
        controls.addWidget(self.time_label)
        controls.addStretch()
        controls.addWidget(QLabel("속도"))
        controls.addWidget(self.speed)
        controls.addWidget(QLabel("볼륨"))
        controls.addWidget(self.volume)

        # 이벤트 종류 필터
        filters = QGridLayout()
        filters.setHorizontalSpacing(14)
        self.checks: dict[str, QCheckBox] = {}
        for i, (etype, (name, color)) in enumerate(EVENT_TYPES.items()):
            cb = QCheckBox(name)
            cb.setChecked(etype in self.visible)
            cb.setStyleSheet(f"QCheckBox::indicator {{ border: 1px solid {color}; border-radius: 3px; }}"
                             f"QCheckBox::indicator:checked {{ background: {color}; }}")
            cb.toggled.connect(self._filters_changed)
            self.checks[etype] = cb
            filters.addWidget(cb, i // 6, i % 6)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.title)
        lv.addWidget(self.video, 1)
        lv.addWidget(self.timeline)
        lv.addLayout(controls)
        lv.addLayout(filters)

        self.event_list = QListWidget()
        self.event_list.setMinimumWidth(260)
        self.event_list.itemActivated.connect(lambda it: self.jump_to_event(it.data(Qt.UserRole)))
        self.event_list.itemClicked.connect(lambda it: self.jump_to_event(it.data(Qt.UserRole)))
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("이벤트"))
        rv.addWidget(self.event_list)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([900, 280])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)

        # 단축키: 스페이스 재생/정지, 좌우 5초, 쉼표/마침표 이벤트 이동
        for key, fn in (("Space", self.toggle_play), ("Left", lambda: self.seek(self.current() - 5)),
                        ("Right", lambda: self.seek(self.current() + 5)), (",", lambda: self.step_event(-1)),
                        (".", lambda: self.step_event(1))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

    # ------------------------------------------------------------------ loading
    def load(self, video: Path | None, events_file: Path | None, title: str) -> None:
        self.player.stop()
        self.title.setText(title)
        self.events = []
        if events_file and events_file.exists():
            self.events, _, _ = load_events(events_file)
        self.events = sorted((e for e in self.events if e.video_time is not None), key=lambda e: e.video_time)
        self.timeline.set_events(self.events, self.visible)
        self._fill_list()
        if video and video.exists():
            self.player.setSource(QUrl.fromLocalFile(str(video)))
            self.player.pause()  # 첫 프레임 표시
        else:
            self.player.setSource(QUrl())
            self.timeline.set_duration(0)

    def unload(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())

    def _fill_list(self) -> None:
        self.event_list.clear()
        for e in self.events:
            if e.type not in self.visible:
                continue
            color = EVENT_TYPES.get(e.type, ("", "#888"))[1]
            item = QListWidgetItem(f"{fmt_time(e.video_time or 0)}   {e.label}")
            item.setData(Qt.UserRole, e)
            item.setForeground(QColor("#ffffff"))
            item.setIcon(_dot_icon(color))
            self.event_list.addItem(item)

    def _filters_changed(self) -> None:
        self.visible = {t for t, cb in self.checks.items() if cb.isChecked()}
        self.timeline.set_visible_types(self.visible)
        self._fill_list()

    # ------------------------------------------------------------------ playback
    def current(self) -> float:
        return self.player.position() / 1000.0

    def seek(self, sec: float) -> None:
        dur = self.player.duration() / 1000.0
        sec = max(0.0, min(sec, dur if dur > 0 else sec))
        self.player.setPosition(int(sec * 1000))

    def jump_to_event(self, e: GameEvent | None) -> None:
        if e is None or e.video_time is None:
            return
        self.seek(e.video_time - self.seek_lead)
        self.player.play()

    def step_event(self, direction: int) -> None:
        shown = sorted((e for e in self.events if e.type in self.visible), key=lambda e: e.video_time or 0)
        if not shown:
            return
        # 이벤트로 점프하면 lead 만큼 앞에서 재생되므로 기준 위치를 보정
        now = self.current() + self.seek_lead
        if direction > 0:
            nxt = next((e for e in shown if (e.video_time or 0) > now + 0.5), None)
        else:
            nxt = next((e for e in reversed(shown) if (e.video_time or 0) < now - 1.0), None)
        if nxt:
            self.jump_to_event(nxt)

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_position(self, ms: int) -> None:
        self.timeline.set_position(ms / 1000.0)
        self.time_label.setText(f"{fmt_time(ms / 1000)} / {fmt_time(self.player.duration() / 1000)}")

    def _on_duration(self, ms: int) -> None:
        self.timeline.set_duration(ms / 1000.0)

    def _on_state(self, state) -> None:
        self.btn_play.setText("❚❚" if state == QMediaPlayer.PlayingState else "▶")
