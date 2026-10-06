"""메인 창: 녹화 목록 + 플레이어 / 대시보드 / 트레이."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSplitter, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from .. import APP_DISPLAY_NAME, __version__
from ..analysis import queue_name
from ..config import Settings, save_settings
from ..exporter import export_games
from ..fetcher import MatchFetcher
from ..storage import GameRow, Storage
from ..watcher import GameWatcher, PipelineCache
from . import theme
from .dashboard import Dashboard
from .overlay import ReplayOverlay
from .player import PlayerView
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

API_STATUS = {"done": "✔", "pending": "대기", "failed": "실패", "no_key": "키 없음"}
VIDEO_STATUS = {"ready": "✔", "recording": "녹화 중", "processing": "정리 중", "failed": "실패", "none": "-"}


def make_icon(recording: bool = False) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1f2a3a"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    p.setBrush(QColor("#e66767" if recording else "#3987e5"))
    p.drawEllipse(QRectF(18, 18, 28, 28))
    p.end()
    return QIcon(pm)


def open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


def set_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}" --minimized'
            else:
                cmd = f'"{sys.executable}" -m lolrec --minimized'
            winreg.SetValueEx(key, "LoLRecorder", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "LoLRecorder")
            except FileNotFoundError:
                pass


class MainWindow(QMainWindow):
    COLUMNS = ["날짜", "챔피언", "큐", "결과", "KDA", "길이", "영상", "데이터"]

    def __init__(self, settings: Settings, storage: Storage, screen_size: tuple[int, int]):
        super().__init__()
        self.settings = settings
        self.storage = storage
        self.setWindowTitle(f"{APP_DISPLAY_NAME} {__version__}")
        self.setWindowIcon(make_icon())
        self.resize(1400, 860)
        self._quitting = False

        self.pipelines = PipelineCache(settings, screen_size)
        self.watcher = GameWatcher(settings, storage, self.pipelines)
        self.fetcher = MatchFetcher(settings, storage)
        self.watcher.on_game_processed = self.fetcher.enqueue
        self.overlay = ReplayOverlay(settings)

        # ---- 녹화 목록 + 플레이어
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        self.player = PlayerView(settings.seek_lead_sec)
        self._loaded: tuple[int, str | None] | None = None  # 플레이어에 열린 (game id, 영상 경로)

        btn_import = QPushButton("과거 경기 가져오기")
        btn_import.clicked.connect(self._import_recent)
        btn_export = QPushButton("Raw 데이터 내보내기")
        btn_export.setObjectName("primary")
        btn_export.clicked.connect(self._export)
        btn_folder = QPushButton("녹화 폴더")
        btn_folder.clicked.connect(lambda: open_folder(self.settings.recordings_path))
        btn_settings = QPushButton("설정")
        btn_settings.clicked.connect(self._open_settings)
        btns = QHBoxLayout()
        for b in (btn_import, btn_export, btn_folder, btn_settings):
            btns.addWidget(b)
        btns.addStretch()

        list_panel = QWidget()
        lv = QVBoxLayout(list_panel)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addLayout(btns)
        hint = QLabel("여러 경기를 선택(Ctrl/Shift)해서 내보낼 수 있습니다. 선택 없이 내보내면 전체를 내보냅니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        lv.addWidget(hint)
        lv.addWidget(self.table)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(list_panel)
        split.addWidget(self.player)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setSizes([460, 940])

        self.dashboard = Dashboard(settings, storage)
        self.tabs = QTabWidget()
        self.tabs.addTab(split, "녹화")
        self.tabs.addTab(self.dashboard, "대시보드")
        self.tabs.currentChanged.connect(lambda i: self.dashboard.refresh() if i == 1 else None)
        self.setCentralWidget(self.tabs)

        self.rec_label = QLabel("")
        self.statusBar().addPermanentWidget(self.rec_label)

        # ---- 트레이
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(APP_DISPLAY_NAME)
        menu = QMenu()
        act_open = QAction("열기", self)
        act_open.triggered.connect(self.show_normal)
        act_quit = QAction("종료", self)
        act_quit.triggered.connect(self.quit)
        menu.addAction(act_open)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_normal() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

        # ---- 시그널
        self.watcher.status_changed.connect(self._set_status)
        self.watcher.error.connect(self._show_error)
        self.watcher.recording_started.connect(self._on_recording_started)
        self.watcher.recording_finished.connect(self._on_recording_finished)
        self.watcher.death_replay_ready.connect(self.overlay.play_clip)
        self.watcher.respawned.connect(self.overlay.finish)
        self.fetcher.status.connect(self._set_status)
        self.fetcher.error.connect(self._show_error)
        self.fetcher.game_updated.connect(self._on_game_updated)
        self.fetcher.import_finished.connect(lambda _: self.refresh_list())

        self.refresh_list()
        self.watcher.start()
        self.fetcher.start()

    # ------------------------------------------------------------------ list
    def refresh_list(self) -> None:
        """목록을 다시 그린다. 선택/스크롤/재생 상태는 유지한다."""
        selected = set(self._selected_ids())
        scroll = self.table.verticalScrollBar().value()
        self.table.blockSignals(True)  # 다시 그리는 동안 선택 변경으로 영상이 다시 열리지 않게
        try:
            self._fill_table(selected)
        finally:
            self.table.blockSignals(False)
        self.table.verticalScrollBar().setValue(scroll)
        if set(self._selected_ids()) != selected:  # 선택했던 경기가 사라진 경우
            self._on_select()

    def _fill_table(self, selected: set[int]) -> None:
        self.table.setRowCount(0)
        selection = QItemSelection()
        for g in self.storage.list_games():
            r = self.table.rowCount()
            self.table.insertRow(r)
            dt = g.started_dt
            dur = g.duration_sec or 0
            result = "-" if g.win is None else ("승리" if g.win else "패배")
            values = [
                dt.strftime("%m/%d %H:%M") if dt else "-",
                (g.champion or "-") + (f" (이어서 {g.session})" if g.session > 1 else ""),
                queue_name(g.queue_id),
                result,
                g.kda_text,
                f"{int(dur // 60)}:{int(dur % 60):02d}" if dur else "-",
                VIDEO_STATUS.get(g.status, g.status) if g.status != "ready" or g.has_video else "삭제됨",
                API_STATUS.get(g.api_status, g.api_status),
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, g.id)
                if c == 3 and g.win is not None:
                    item.setForeground(QColor(theme.WIN if g.win else theme.LOSS))
                if g.note:
                    item.setToolTip(g.note)
                self.table.setItem(r, c, item)
            if g.id in selected:
                model = self.table.model()
                selection.select(model.index(r, 0), model.index(r, len(self.COLUMNS) - 1))
        if not selection.isEmpty():
            self.table.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect)

    def _selected_ids(self) -> list[int]:
        rows = {i.row() for i in self.table.selectedIndexes()}
        out = []
        for r in sorted(rows):
            item = self.table.item(r, 0)
            if item is not None:
                out.append(item.data(Qt.UserRole))
        return out

    def _selected_games(self) -> list[GameRow]:
        return [g for g in (self.storage.get_game(i) for i in self._selected_ids()) if g]

    def _on_select(self, reload_events: bool = False) -> None:
        """선택한 경기를 플레이어에 연다. 이미 열린 영상이면 재생 상태를 건드리지 않는다."""
        games = self._selected_games()
        if len(games) != 1:
            return
        g = games[0]
        title = f"{g.champion or '?'} · {queue_name(g.queue_id)} · {g.kda_text}"
        if g.win is not None:
            title += " · " + ("승리" if g.win else "패배")
        folder = Path(g.folder) if g.folder else None
        events_file = folder / "events.json" if folder else None
        video = g.video_path if g.has_video else None
        key = (g.id, video)
        if key == self._loaded:
            if reload_events:
                self.player.reload_events(events_file, title)
                self.dashboard.show_gold_diff(g.match_id, g.puuid or "", g.riot_id or "")
            return
        self._loaded = key
        self.player.load(Path(video) if video else None, events_file, title)
        self.dashboard.show_gold_diff(g.match_id, g.puuid or "", g.riot_id or "")

    def _context_menu(self, pos) -> None:
        games = self._selected_games()
        if not games:
            return
        menu = QMenu(self)
        if len(games) == 1 and games[0].folder:
            menu.addAction("폴더 열기", lambda: open_folder(Path(games[0].folder)))
        menu.addAction("데이터 다시 받기", lambda: [self.fetcher.enqueue(g.id, immediate=True) for g in games])
        menu.addAction("영상 삭제 (데이터 유지)", lambda: self._delete(games, keep_data=True))
        menu.addAction("완전히 삭제", lambda: self._delete(games, keep_data=False))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _delete(self, games: list[GameRow], keep_data: bool) -> None:
        if keep_data:
            msg = f"선택한 {len(games)}개 경기의 영상을 삭제할까요?\n경기 기록과 원본 데이터는 남습니다."
        else:
            msg = (f"선택한 {len(games)}개 경기를 완전히 삭제할까요?\n"
                   "영상, 녹화 폴더(이벤트 포함), 경기 기록, 원본 API 데이터(match/timeline JSON)가 모두 삭제됩니다.")
        if QMessageBox.question(self, "삭제", msg) != QMessageBox.Yes:
            return
        self.player.unload()
        self._loaded = None
        skipped = 0
        for g in games:
            if g.status in ("recording", "processing"):
                skipped += 1
                continue
            if g.folder and Path(g.folder).exists():
                if keep_data:
                    if g.video_path:
                        Path(g.video_path).unlink(missing_ok=True)
                else:
                    shutil.rmtree(g.folder, ignore_errors=True)
            if keep_data:
                self.storage.update_game(g.id, video_path=None, status="none")
            else:
                self.storage.delete_game(g.id)
                # 같은 경기를 이어서 녹화한 다른 행이 없을 때만 원본 데이터 삭제
                if g.match_id and self.storage.find_by_match_id(g.match_id) is None:
                    self.storage.delete_raw(g.match_id)
        self.refresh_list()
        if skipped:
            self._set_status(f"녹화/정리 중인 {skipped}개 경기는 삭제하지 않았습니다")

    # ------------------------------------------------------------------ actions
    def _import_recent(self) -> None:
        count, ok = QInputDialog.getInt(self, "과거 경기 가져오기", "최근 몇 경기를 가져올까요? (영상 없이 데이터만)",
                                        20, 1, 100)
        if ok:
            self.fetcher.import_recent(count)
            self._set_status("과거 경기 가져오는 중...")

    def _export(self) -> None:
        games = self._selected_games() or self.storage.list_games()
        if not games:
            QMessageBox.information(self, "내보내기", "내보낼 경기가 없습니다")
            return
        d = QFileDialog.getExistingDirectory(self, "내보낼 폴더 선택")
        if not d:
            return
        from datetime import datetime

        out = Path(d) / f"lol_export_{datetime.now():%Y%m%d_%H%M%S}"
        counts = export_games(self.storage, games, out, self.settings.puuid)
        summary = "\n".join(f"  {k}: {v}행" for k, v in counts.items())
        QMessageBox.information(self, "내보내기 완료", f"{len(games)}개 경기를 내보냈습니다.\n{out}\n\n{summary}")
        open_folder(out)

    def _open_settings(self) -> None:
        old = (self.settings.encoder, self.settings.fps, self.settings.bitrate_kbps, self.settings.resolution,
               self.settings.monitor_index)
        old_dir = self.settings.recordings_dir
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            if self.settings.recordings_dir != old_dir:
                self._move_raw_data()
            new = (self.settings.encoder, self.settings.fps, self.settings.bitrate_kbps, self.settings.resolution,
                   self.settings.monitor_index)
            if old != new:
                self.pipelines.invalidate()
            self.player.seek_lead = self.settings.seek_lead_sec
            save_settings(self.settings)
            try:
                set_autostart(self.settings.launch_on_startup)
            except OSError:
                log.exception("자동 실행 설정 실패")
            if self.watcher.recording and old != new:
                self._set_status("녹화 설정 변경은 다음 게임부터 적용됩니다")

    def _move_raw_data(self) -> None:
        """저장 위치가 바뀌면 원본 API 데이터도 새 위치로 옮긴다 (기존 녹화 폴더는 그대로 둠)."""
        new_dir = self.settings.raw_data_path
        try:
            moved = self.storage.relocate_data(new_dir)
        except OSError as e:
            log.exception("원본 데이터 이동 실패")
            QMessageBox.warning(self, "저장 위치", f"원본 데이터를 새 위치로 옮기지 못했습니다: {e}\n"
                                                  "다음 실행 때 다시 시도합니다.")
            return
        self.settings.raw_data_dir = str(new_dir)
        QMessageBox.information(self, "저장 위치",
                                f"새 녹화는 {self.settings.recordings_path} 에 저장됩니다.\n"
                                f"원본 경기 데이터 {moved}개를 새 위치로 옮겼습니다.\n"
                                "기존 녹화 영상은 원래 폴더에 그대로 남아 있습니다.")

    # ------------------------------------------------------------------ signals
    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)
        self.tray.setToolTip(f"{APP_DISPLAY_NAME} - {text}")

    def _show_error(self, text: str) -> None:
        self._set_status(text)
        self.tray.showMessage(APP_DISPLAY_NAME, text, QSystemTrayIcon.Warning, 5000)

    def _on_recording_started(self, _gid: int) -> None:
        self.rec_label.setText("● REC")
        self.rec_label.setStyleSheet(f"color: {theme.LOSS}; font-weight: 700; padding-right: 8px;")
        self.tray.setIcon(make_icon(True))
        self.refresh_list()

    def _on_recording_finished(self, gid: int) -> None:
        self.rec_label.setText("")
        self.tray.setIcon(make_icon(False))
        self.refresh_list()
        if self._selected_ids() == [gid]:
            self._on_select()  # 정리가 끝나 영상이 생겼으면 연다

    def _on_game_updated(self, gid: int) -> None:
        self.refresh_list()
        if self.tabs.currentIndex() == 1:
            self.dashboard.refresh()
        if self._selected_ids() == [gid]:
            # 타임라인 이벤트가 추가됐을 수 있으니 이벤트만 다시 읽기 (재생 위치/상태 유지)
            self._on_select(reload_events=True)

    # ------------------------------------------------------------------ window
    def show_normal(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        if self._quitting:
            event.accept()
            return
        # X 버튼은 트레이로 숨기기 (녹화는 계속)
        event.ignore()
        self.player.player.pause()
        self.hide()
        self.tray.showMessage(APP_DISPLAY_NAME, "트레이에서 계속 실행됩니다. 종료하려면 트레이 아이콘 → 종료",
                              QSystemTrayIcon.Information, 3000)

    def quit(self) -> None:
        if self.watcher.recording:
            if QMessageBox.question(self, "종료", "녹화 중입니다. 종료하면 지금까지 녹화된 부분만 저장됩니다. 종료할까요?") \
                    != QMessageBox.Yes:
                return
        self._quitting = True
        self._set_status("종료 중...")
        self.watcher.stop()
        self.fetcher.stop()
        self.tray.hide()
        from PySide6.QtWidgets import QApplication

        QApplication.quit()
