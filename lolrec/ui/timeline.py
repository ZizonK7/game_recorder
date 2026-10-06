"""이벤트 마커가 있는 재생바."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from ..events import EVENT_TYPES, GameEvent
from . import theme

# 마커 줄 배치: 내 이벤트는 위, 오브젝트는 가운데, 기타는 아래
ROW_OF = {
    "kill": 0, "death": 0, "assist": 0, "multikill": 0, "first_blood": 0, "ace": 0,
    "dragon": 1, "baron": 1, "herald": 1, "atakhan": 1, "turret": 1, "inhibitor": 1, "game_end": 1,
    "ally_kill": 2, "enemy_kill": 2, "ward": 2, "item": 2, "level": 2,
}
ROWS = 3


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class TimelineBar(QWidget):
    seek_requested = Signal(float)   # 초
    event_clicked = Signal(object)   # GameEvent

    MARKER_H = 14
    BAR_H = 8
    PAD = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(self.PAD * 2 + ROWS * (self.MARKER_H + 2) + self.BAR_H + 16)
        self.duration = 0.0
        self.position = 0.0
        self.events: list[GameEvent] = []
        self.visible_types: set[str] = set()
        self._hover_x: float | None = None
        self._dragging = False

    # ------------------------------------------------------------------ data
    def set_events(self, events: list[GameEvent], visible: set[str]) -> None:
        self.events = [e for e in events if e.video_time is not None]
        self.visible_types = visible
        self.update()

    def set_visible_types(self, visible: set[str]) -> None:
        self.visible_types = visible
        self.update()

    def set_duration(self, sec: float) -> None:
        self.duration = max(0.0, sec)
        self.update()

    def set_position(self, sec: float) -> None:
        self.position = sec
        if not self._dragging:
            self.update()

    def shown_events(self) -> list[GameEvent]:
        return [e for e in self.events if e.type in self.visible_types]

    # ------------------------------------------------------------------ geometry
    def _track(self) -> QRectF:
        top = self.PAD + ROWS * (self.MARKER_H + 2)
        return QRectF(self.PAD, top + 4, self.width() - 2 * self.PAD, self.BAR_H)

    def _x_for(self, t: float) -> float:
        r = self._track()
        if self.duration <= 0:
            return r.left()
        return r.left() + r.width() * min(max(t / self.duration, 0.0), 1.0)

    def _t_for(self, x: float) -> float:
        r = self._track()
        if r.width() <= 0:
            return 0.0
        return min(max((x - r.left()) / r.width(), 0.0), 1.0) * self.duration

    def _marker_rect(self, e: GameEvent) -> QRectF:
        x = self._x_for(e.video_time or 0)
        row = ROW_OF.get(e.type, 2)
        y = self.PAD + row * (self.MARKER_H + 2)
        return QRectF(x - 5, y, 10, self.MARKER_H)

    def _event_at(self, pos: QPointF) -> GameEvent | None:
        best, best_d = None, 1e9
        for e in self.shown_events():
            r = self._marker_rect(e).adjusted(-3, -2, 3, 2)
            if r.contains(pos):
                d = abs(r.center().x() - pos.x())
                if d < best_d:
                    best, best_d = e, d
        return best

    # ------------------------------------------------------------------ paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.SURFACE))
        track = self._track()

        # 줄 안내선
        p.setPen(QPen(QColor(theme.GRID), 1))
        for row in range(ROWS):
            y = self.PAD + row * (self.MARKER_H + 2) + self.MARKER_H / 2
            p.drawLine(QPointF(track.left(), y), QPointF(track.right(), y))

        # 트랙
        path = QPainterPath()
        path.addRoundedRect(track, 4, 4)
        p.fillPath(path, QColor(theme.TRACK))
        if self.duration > 0:
            done = QRectF(track.left(), track.top(), self._x_for(self.position) - track.left(), track.height())
            path2 = QPainterPath()
            path2.addRoundedRect(done, 4, 4)
            p.fillPath(path2, QColor(theme.ACCENT))

        # 마커
        for e in self.shown_events():
            r = self._marker_rect(e)
            color = QColor(EVENT_TYPES.get(e.type, ("", "#888888"))[1])
            mp = QPainterPath()
            mp.addRoundedRect(r, 3, 3)
            p.fillPath(mp, color)
            p.setPen(QPen(QColor(theme.SURFACE), 1))
            p.drawPath(mp)
            # 트랙까지 가는 가는 선
            p.setPen(QPen(color, 1))
            p.drawLine(QPointF(r.center().x(), r.bottom()), QPointF(r.center().x(), track.top()))

        # 재생 위치
        x = self._x_for(self.position)
        p.setPen(QPen(QColor(theme.TEXT), 2))
        p.drawLine(QPointF(x, self.PAD - 4), QPointF(x, track.bottom() + 3))

        # 시간 표시
        p.setPen(QColor(theme.TEXT_MUTED))
        p.drawText(QRectF(track.left(), track.bottom() + 2, 80, 14), Qt.AlignLeft, fmt_time(self.position))
        p.drawText(QRectF(track.right() - 80, track.bottom() + 2, 80, 14), Qt.AlignRight, fmt_time(self.duration))

        # 마우스 위치 시간
        if self._hover_x is not None and self.duration > 0:
            hx = self._hover_x
            p.setPen(QPen(QColor(theme.TEXT_MUTED), 1, Qt.DashLine))
            p.drawLine(QPointF(hx, self.PAD - 4), QPointF(hx, track.bottom()))
            p.drawText(QRectF(hx - 40, track.bottom() + 2, 80, 14), Qt.AlignCenter, fmt_time(self._t_for(hx)))
        p.end()

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, e: QMouseEvent):
        if e.button() != Qt.LeftButton or self.duration <= 0:
            return
        hit = self._event_at(e.position())
        if hit is not None:
            self.event_clicked.emit(hit)
            return
        self._dragging = True
        self.position = self._t_for(e.position().x())
        self.update()

    def mouseMoveEvent(self, e: QMouseEvent):
        self._hover_x = e.position().x()
        if self._dragging:
            self.position = self._t_for(e.position().x())
        hit = self._event_at(e.position())
        if hit is not None:
            QToolTip.showText(e.globalPosition().toPoint(), f"{fmt_time(hit.video_time or 0)}  {hit.label}", self)
            self.setCursor(Qt.PointingHandCursor)
        else:
            QToolTip.hideText()
            self.setCursor(Qt.ArrowCursor)
        self.update()

    def mouseReleaseEvent(self, e: QMouseEvent):
        if self._dragging:
            self._dragging = False
            self.seek_requested.emit(self.position)

    def leaveEvent(self, _):
        self._hover_x = None
        self.update()
