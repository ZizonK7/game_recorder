"""기본 데이터 대시보드."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QScatterSeries, QValueAxis
from PySide6.QtCore import QMargins, QPointF, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QToolTip,
    QVBoxLayout, QWidget,
)

from .. import analysis
from ..config import Settings
from ..storage import Storage
from . import theme

METRICS = {
    "KDA": lambda g: g.kda,
    "CS/분": lambda g: g.cs_per_min,
    "분당 딜량": lambda g: g.damage_per_min,
    "분당 골드": lambda g: g.gold_per_min,
    "시야 점수": lambda g: g.vision,
    "킬 관여율(%)": lambda g: g.kill_participation * 100,
}
QUEUE_FILTERS = {"전체": None, "솔로랭크": {420}, "자유랭크": {440}, "랭크 전체": {420, 440},
                 "일반": {400, 430, 490}, "칼바람": {450}}


def _tile(label: str) -> tuple[QFrame, QLabel]:
    f = QFrame()
    f.setObjectName("tile")
    v = QVBoxLayout(f)
    v.setContentsMargins(14, 10, 14, 10)
    val = QLabel("-")
    val.setObjectName("tileValue")
    lab = QLabel(label)
    lab.setObjectName("tileLabel")
    v.addWidget(val)
    v.addWidget(lab)
    return f, val


def _style_chart(chart: QChart, title: str) -> None:
    chart.setTitle(title)
    chart.setTitleBrush(QColor(theme.TEXT))
    f = QFont()
    f.setPointSize(10)
    f.setBold(True)
    chart.setTitleFont(f)
    chart.setBackgroundBrush(QColor(theme.SURFACE_2))
    chart.setPlotAreaBackgroundVisible(False)
    chart.setMargins(QMargins(6, 6, 6, 6))
    chart.legend().setLabelColor(QColor(theme.TEXT_SECONDARY))
    chart.legend().setAlignment(Qt.AlignTop)


def _style_axis(axis) -> None:
    axis.setLabelsColor(QColor(theme.TEXT_MUTED))
    axis.setGridLineColor(QColor(theme.GRID))
    axis.setLinePenColor(QColor(theme.GRID))


class Dashboard(QWidget):
    def __init__(self, settings: Settings, storage: Storage, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.storage = storage
        self.summaries: list[analysis.GameSummary] = []

        self.queue_filter = QComboBox()
        self.queue_filter.addItems(QUEUE_FILTERS)
        self.count_filter = QComboBox()
        self.count_filter.addItems(["최근 20판", "최근 50판", "최근 100판", "전체"])
        self.metric = QComboBox()
        self.metric.addItems(METRICS)
        for c in (self.queue_filter, self.count_filter, self.metric):
            c.currentIndexChanged.connect(self.refresh)
        top = QHBoxLayout()
        top.addWidget(QLabel("큐"))
        top.addWidget(self.queue_filter)
        top.addWidget(QLabel("범위"))
        top.addWidget(self.count_filter)
        top.addWidget(QLabel("추세 지표"))
        top.addWidget(self.metric)
        top.addStretch()
        self.empty_note = QLabel("")
        self.empty_note.setObjectName("muted")
        top.addWidget(self.empty_note)

        tiles = QGridLayout()
        self.tile_vals: dict[str, QLabel] = {}
        for i, name in enumerate(["경기", "승률", "KDA", "평균 K / D / A", "CS/분", "분당 딜량", "분당 골드",
                                  "시야 점수", "킬 관여율"]):
            frame, val = _tile(name)
            self.tile_vals[name] = val
            tiles.addWidget(frame, 0, i)

        # 추세 차트
        self.trend_chart = QChart()
        self.trend_view = QChartView(self.trend_chart)
        self.trend_view.setRenderHint(QPainter.Antialiasing)
        self.trend_view.setMinimumHeight(260)

        # 라인전 골드 차이 차트 (선택한 경기)
        self.gold_chart = QChart()
        self.gold_view = QChartView(self.gold_chart)
        self.gold_view.setRenderHint(QPainter.Antialiasing)
        self.gold_view.setMinimumHeight(260)

        # 챔피언 표
        self.champ_table = QTableWidget(0, 5)
        self.champ_table.setHorizontalHeaderLabels(["챔피언", "판수", "승률", "KDA", "CS/분"])
        self.champ_table.verticalHeader().setVisible(False)
        self.champ_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.champ_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.champ_table.setAlternatingRowColors(True)

        charts = QHBoxLayout()
        charts.addWidget(self.trend_view, 3)
        charts.addWidget(self.gold_view, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(tiles)
        layout.addLayout(charts, 3)
        layout.addWidget(QLabel("챔피언별 기록"))
        layout.addWidget(self.champ_table, 2)
        self.show_gold_diff(None)

    # ------------------------------------------------------------------ data
    def _load(self) -> list[analysis.GameSummary]:
        out = []
        queues = QUEUE_FILTERS[self.queue_filter.currentText()]
        seen: set[str] = set()
        for g in self.storage.list_games():
            # 같은 경기를 나눠 녹화한 행(session 2 ...)은 한 번만 집계
            if not g.match_id or g.api_status != "done" or g.match_id in seen:
                continue
            seen.add(g.match_id)
            match = self.storage.load_match(g.match_id)
            if not match:
                continue
            s = analysis.summarize(match, g.puuid or self.settings.puuid, g.riot_id or self.settings.riot_id)
            if s is None or (queues and s.queue_id not in queues):
                continue
            out.append(s)
        out.sort(key=lambda s: s.game_start, reverse=True)
        limit = {0: 20, 1: 50, 2: 100}.get(self.count_filter.currentIndex())
        return out[:limit] if limit else out

    def refresh(self) -> None:
        self.summaries = self._load()
        ov = analysis.overview(self.summaries)
        self.empty_note.setText("" if self.summaries else
                                "경기 데이터가 없습니다. 게임을 하거나 '과거 경기 가져오기'를 눌러주세요.")
        t = self.tile_vals
        t["경기"].setText(f"{ov.games}  ({ov.wins}승 {ov.games - ov.wins}패)")
        t["승률"].setText(f"{ov.win_rate * 100:.1f}%")
        t["KDA"].setText(f"{ov.avg_kda:.2f}")
        t["평균 K / D / A"].setText(f"{ov.avg_kills:.1f} / {ov.avg_deaths:.1f} / {ov.avg_assists:.1f}")
        t["CS/분"].setText(f"{ov.avg_cs_per_min:.1f}")
        t["분당 딜량"].setText(f"{ov.avg_damage_per_min:,.0f}")
        t["분당 골드"].setText(f"{ov.avg_gold_per_min:,.0f}")
        t["시야 점수"].setText(f"{ov.avg_vision:.1f}")
        t["킬 관여율"].setText(f"{ov.avg_kp * 100:.0f}%")
        self._draw_trend()
        self._fill_champs()

    def _fill_champs(self) -> None:
        stats = analysis.champion_stats(self.summaries)
        self.champ_table.setRowCount(len(stats))
        for r, s in enumerate(stats):
            cs = sum(s.cs_per_min) / len(s.cs_per_min) if s.cs_per_min else 0
            for c, val in enumerate([s.champion, str(s.games), f"{s.win_rate * 100:.0f}%", f"{s.kda:.2f}",
                                     f"{cs:.1f}"]):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignCenter)
                self.champ_table.setItem(r, c, item)

    def _draw_trend(self) -> None:
        chart = self.trend_chart
        chart.removeAllSeries()
        for ax in chart.axes():
            chart.removeAxis(ax)
        name = self.metric.currentText()
        f = METRICS[name]
        games = [g for g in reversed(self.summaries) if not g.remake]  # 오래된 경기 -> 최근 경기
        _style_chart(chart, f"경기별 {name} (왼쪽이 오래된 경기)")

        line = QLineSeries()
        line.setName(name)
        line.setPen(QPen(QColor(theme.TEXT_MUTED), 2))
        wins, losses = QScatterSeries(), QScatterSeries()
        wins.setName("승리")
        losses.setName("패배")
        for s, color in ((wins, theme.WIN), (losses, theme.LOSS)):
            s.setColor(QColor(color))
            s.setBorderColor(QColor(theme.SURFACE_2))
            s.setMarkerSize(10)
        for i, g in enumerate(games):
            v = f(g)
            line.append(i, v)
            (wins if g.win else losses).append(i, v)
        for s in (line, wins, losses):
            chart.addSeries(s)
        chart.legend().markers(line)[0].setVisible(False)

        ax_x = QValueAxis()
        ax_x.setRange(-0.5, max(len(games) - 0.5, 0.5))
        ax_x.setLabelsVisible(False)
        ax_x.setGridLineVisible(False)
        ax_y = QValueAxis()
        values = [f(g) for g in games] or [0, 1]
        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.15 or 1
        ax_y.setRange(max(0, lo - pad), hi + pad)
        ax_y.setLabelFormat("%.1f")
        for ax in (ax_x, ax_y):
            _style_axis(ax)
        chart.addAxis(ax_x, Qt.AlignBottom)
        chart.addAxis(ax_y, Qt.AlignLeft)
        for s in (line, wins, losses):
            s.attachAxis(ax_x)
            s.attachAxis(ax_y)

        def tooltip(point: QPointF, state: bool):
            if not state:
                QToolTip.hideText()
                return
            i = int(round(point.x()))
            if 0 <= i < len(games):
                g = games[i]
                when = datetime.fromtimestamp(g.game_start / 1000).strftime("%m/%d %H:%M") if g.game_start else ""
                QToolTip.showText(QCursor.pos(), f"{when}  {g.champion}  {'승' if g.win else '패'}\n"
                                                 f"{g.kills}/{g.deaths}/{g.assists}  {name}: {f(g):.2f}")

        for s in (wins, losses):
            s.hovered.connect(tooltip)

    def show_gold_diff(self, match_id: str | None, puuid: str = "", riot_id: str = "") -> None:
        chart = self.gold_chart
        chart.removeAllSeries()
        for ax in chart.axes():
            chart.removeAxis(ax)
        _style_chart(chart, "라인 상대와 골드 차이 (선택한 경기)")
        chart.legend().setVisible(False)
        data: list[tuple[int, int]] = []
        if match_id:
            match, tl = self.storage.load_match(match_id), self.storage.load_timeline(match_id)
            if match and tl:
                data = analysis.lane_gold_diff(match, tl, puuid or self.settings.puuid, riot_id or self.settings.riot_id)
        if not data:
            chart.setTitle("라인 상대와 골드 차이 - 경기를 선택하세요 (API 데이터 필요)")
            return
        zero = QLineSeries()
        line = QLineSeries()
        line.setPen(QPen(QColor(theme.ACCENT), 2))
        for m, d in data:
            line.append(m, d)
        zero.setPen(QPen(QColor(theme.TEXT_MUTED), 1, Qt.DashLine))
        zero.append(0, 0)
        zero.append(data[-1][0], 0)
        chart.addSeries(zero)
        chart.addSeries(line)
        ax_x, ax_y = QValueAxis(), QValueAxis()
        ax_x.setRange(0, data[-1][0])
        ax_x.setLabelFormat("%d")
        ax_x.setTitleText("분")
        ax_x.setTitleBrush(QColor(theme.TEXT_MUTED))
        ax_x.setTickCount(min(8, len(data)) or 2)
        lim = max(abs(d) for _, d in data) * 1.15 or 1000
        ax_y.setRange(-lim, lim)
        ax_y.setLabelFormat("%+.0f")
        for ax in (ax_x, ax_y):
            _style_axis(ax)
        chart.addAxis(ax_x, Qt.AlignBottom)
        chart.addAxis(ax_y, Qt.AlignLeft)
        for s in (zero, line):
            s.attachAxis(ax_x)
            s.attachAxis(ax_y)

        def tooltip(point: QPointF, state: bool):
            if state:
                QToolTip.showText(QCursor.pos(), f"{int(round(point.x()))}분: {point.y():+,.0f} 골드")
            else:
                QToolTip.hideText()

        line.hovered.connect(tooltip)
