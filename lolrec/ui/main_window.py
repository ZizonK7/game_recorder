"""메인 창: 녹화 목록 + 플레이어 / 대시보드 / 트레이."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import dataclasses
import threading

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMainWindow, QMenu,
    QMessageBox, QProgressDialog, QPushButton, QSplitter, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from .. import APP_DISPLAY_NAME, __version__
from ..analysis import queue_name
from ..config import Settings, save_settings
from ..exporter import export_games
from ..fetcher import MatchFetcher
from ..storage import GameRow, Storage
from ..watcher import (
    GameWatcher, PipelineCache, delete_videos, plan_keep_recent, plan_storage_limit,
)
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


def screen_pixel_size(monitor_index: int) -> tuple[int, int]:
    """설정한 모니터의 물리 해상도 (배율 적용 전 픽셀). 없는 번호면 첫 번째 모니터."""
    screens = QGuiApplication.screens()
    screen = screens[monitor_index] if 0 <= monitor_index < len(screens) else QGuiApplication.primaryScreen()
    ratio = screen.devicePixelRatio()
    geo = screen.geometry()
    return int(round(geo.width() * ratio)), int(round(geo.height() * ratio))


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
    _shutdown_done = Signal()

    def __init__(self, settings: Settings, storage: Storage, screen_size: tuple[int, int]):
        super().__init__()
        self.settings = settings
        self.storage = storage
        self.setWindowTitle(f"{APP_DISPLAY_NAME} {__version__}")
        self.setWindowIcon(make_icon())
        self.resize(1400, 860)
        self._quitting = False
        self.shutdown_clean = False  # 백그라운드 작업이 모두 끝난 뒤 종료했는지 (DB 를 닫아도 되는지)
        self._quit_dialog: QProgressDialog | None = None
        self._shutdown_done.connect(self._finish_quit)

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
        btn_export = QPushButton("데이터 내보내기")
        btn_export.setToolTip("선택한 경기(없으면 전체)의 원본 API 데이터와 CSV 를 폴더로 내보내기")
        btn_export.clicked.connect(self._export)
        btn_folder = QPushButton("녹화 폴더")
        btn_folder.clicked.connect(lambda: open_folder(self.settings.recordings_path))
        btn_settings = QPushButton("설정")
        btn_settings.clicked.connect(self._open_settings)
        # 버튼은 2x2 로 배치해 목록 패널이 좁아도 되게
        btns = QGridLayout()
        btns.setSpacing(6)
        for i, b in enumerate((btn_folder, btn_settings, btn_import, btn_export)):
            btns.addWidget(b, i // 2, i % 2)

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

        # 모니터 연결/해상도 변경을 다음 녹화에 반영
        app = QGuiApplication.instance()
        app.screenAdded.connect(self._watch_screen)
        app.screenAdded.connect(lambda _s: self._update_screen_size())
        app.screenRemoved.connect(lambda _s: self._update_screen_size())
        for sc in QGuiApplication.screens():
            self._watch_screen(sc)

        self.refresh_list()
        self.watcher.start()
        self.fetcher.start()

    def _watch_screen(self, screen) -> None:
        screen.geometryChanged.connect(lambda _g: self._update_screen_size())

    def _update_screen_size(self) -> None:
        size = screen_pixel_size(self.settings.monitor_index)
        if self.pipelines.set_screen_size(size) and self.watcher.recording:
            self._set_status("모니터 변경은 다음 게임부터 적용됩니다")

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

    PIPELINE_FIELDS = ("encoder", "fps", "bitrate_kbps", "resolution", "monitor_index")
    CLEANUP_FIELDS = ("keep_recent_videos", "max_storage_gb")

    def _open_settings(self) -> None:
        # 설정 창은 사본을 편집한다. 백그라운드 작업(영상 정리 등)은 여기서 반영한 값만 보게 된다.
        snapshot = dataclasses.replace(self.settings)
        draft = dataclasses.replace(self.settings)
        if not SettingsDialog(draft, self).exec():
            return
        # 창이 열려 있는 동안 다른 곳에서 바뀐 값(puuid 등)을 덮어쓰지 않도록, 사용자가 바꾼 값만 반영
        changed = {f.name: getattr(draft, f.name) for f in dataclasses.fields(draft)
                   if getattr(draft, f.name) != getattr(snapshot, f.name)}
        cleanup = None
        if any(k in changed for k in self.CLEANUP_FIELDS):
            cleanup = self._confirm_cleanup(draft)
            if cleanup is None:  # 삭제를 거절하면 자동 삭제 설정은 바꾸지 않는다
                for k in self.CLEANUP_FIELDS:
                    changed.pop(k, None)
        for k, v in changed.items():
            setattr(self.settings, k, v)

        if cleanup:
            self._run_cleanup(cleanup)
        if "recordings_dir" in changed:
            self._move_raw_data()
        pipeline_changed = any(k in changed for k in self.PIPELINE_FIELDS)
        if pipeline_changed:
            self.pipelines.invalidate()
        self._update_screen_size()
        self.player.seek_lead = self.settings.seek_lead_sec
        save_settings(self.settings)
        try:
            set_autostart(self.settings.launch_on_startup)
        except OSError:
            log.exception("자동 실행 설정 실패")
        if self.watcher.recording and pipeline_changed:
            self._set_status("녹화 설정 변경은 다음 게임부터 적용됩니다")

    def _confirm_cleanup(self, draft: Settings) -> list[GameRow] | None:
        """새 자동 삭제 설정으로 지금 지워질 영상을 보여주고 확인받는다.
        지울 영상이 없으면 [], 사용자가 거절하면 None."""
        by_keep = plan_keep_recent(self.storage, draft.keep_recent_videos)
        by_size, _ = plan_storage_limit(self.storage, draft.max_storage_gb)
        targets = {g.id: g for g in by_keep + by_size}
        if not targets:
            return []
        reasons = []
        if by_keep:
            reasons.append(f"최근 {draft.keep_recent_videos}경기 보관: {len(by_keep)}개")
        if by_size:
            reasons.append(f"최대 {draft.max_storage_gb:g} GB 용량 제한: {len(by_size)}개")
        oldest = min((g.started_dt for g in targets.values() if g.started_dt), default=None)
        since = f"\n가장 오래된 영상: {oldest:%Y-%m-%d}" if oldest else ""
        answer = QMessageBox.question(
            self, "영상 자동 삭제",
            f"새 설정을 적용하면 오래된 영상 {len(targets)}개가 지금 삭제됩니다.\n"
            f"({', '.join(reasons)}){since}\n\n"
            "경기 기록과 통계는 유지됩니다. 계속할까요?\n"
            "'아니요'를 누르면 자동 삭제 설정은 바뀌지 않고, 나머지 설정만 저장됩니다.")
        if answer != QMessageBox.Yes:
            return None
        return list(targets.values())

    def _run_cleanup(self, targets: list[GameRow]) -> None:
        if self._loaded and any(g.id == self._loaded[0] for g in targets):
            self.player.unload()  # 재생 중인 파일은 지울 수 없음
            self._loaded = None
        # 사용자가 확인한 영상만 지운다 (이후 새로 생긴 영상은 다음 정리 때 설정대로 처리)
        removed = delete_videos(self.storage, targets, "자동 삭제 설정 변경으로 영상 삭제")
        self.refresh_list()
        self._set_status(f"오래된 영상 {len(removed)}개를 삭제했습니다")

    def _move_raw_data(self) -> None:
        """저장 위치가 바뀌면 원본 API 데이터도 새 위치로 옮긴다 (기존 녹화 폴더는 그대로 둠).
        옮기지 못한 파일은 예전 위치에서 계속 읽고, 다음 실행 때 다시 옮긴다."""
        new_dir = self.settings.raw_data_path
        try:
            result = self.storage.relocate_data(new_dir)
        except OSError as e:
            log.exception("원본 데이터 이동 실패")
            result = None
            error = str(e)
        self.settings.raw_data_dir = str(self.storage.data_dir)
        self.settings.raw_data_fallbacks = [str(d) for d in self.storage.fallback_dirs]
        if result is not None and result.unreachable and not result.failed:
            QMessageBox.warning(
                self, "저장 위치",
                f"원본 경기 데이터 {result.moved}개를 새 위치로 옮겼습니다.\n"
                "다만 지금 접근할 수 없는 예전 위치가 있어 그곳의 데이터는 나중에 옮깁니다 (드라이브 연결 확인):\n"
                + "\n".join(str(d) for d in result.unreachable))
            return
        if result is None or result.failed:
            failed = f"{len(result.failed)}개" if result else "일부"
            detail = f"\n오류: {error}" if result is None else ""
            QMessageBox.warning(
                self, "저장 위치",
                f"원본 경기 데이터 {failed}를 새 위치로 옮기지 못했습니다.{detail}\n"
                "다른 프로그램이 파일을 사용 중일 수 있습니다. 기존 위치에서 계속 읽으며, 다음 실행 때 다시 옮깁니다.\n"
                f"기존 위치: {', '.join(self.settings.raw_data_fallbacks)}")
            return
        QMessageBox.information(self, "저장 위치",
                                f"새 녹화는 {self.settings.recordings_path} 에 저장됩니다.\n"
                                f"원본 경기 데이터 {result.moved}개를 새 위치로 옮겼습니다.\n"
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
        if self._quitting:
            return
        if self.watcher.recording:
            if QMessageBox.question(self, "종료", "녹화 중입니다. 종료하면 지금까지 녹화된 부분만 저장됩니다. 종료할까요?") \
                    != QMessageBox.Yes:
                return
        self._quitting = True
        self._set_status("종료 중...")
        self.player.unload()
        self.overlay.finish()
        if self.watcher.busy:
            # 영상 정리는 몇 분 걸릴 수 있으므로 창을 멈추지 않고 진행 상황만 보여준다
            dlg = QProgressDialog("녹화한 영상을 정리하고 있습니다.\n끝나면 자동으로 종료됩니다.", "강제 종료", 0, 0)
            dlg.setWindowTitle(APP_DISPLAY_NAME)
            dlg.setWindowIcon(make_icon(True))
            dlg.setMinimumDuration(0)
            dlg.canceled.connect(self._force_quit)
            dlg.show()
            self._quit_dialog = dlg
        self.hide()
        threading.Thread(target=self._shutdown_worker, daemon=True, name="shutdown").start()

    def _shutdown_worker(self) -> None:
        try:
            self.watcher.stop()  # 영상 정리까지 끝날 때까지 대기
            self.shutdown_clean = self.fetcher.stop()  # API 작업자가 실제로 끝났는지
        except Exception:
            log.exception("종료 처리 중 오류")
        finally:
            self._shutdown_done.emit()

    def _force_quit(self) -> None:
        if QMessageBox.question(None, APP_DISPLAY_NAME,
                                "지금 종료하면 정리 중인 녹화는 다음 실행 때 녹화 조각에서 복구를 시도합니다.\n"
                                "강제 종료할까요?") != QMessageBox.Yes:
            if self._quit_dialog:
                self._quit_dialog.show()
            return
        log.warning("사용자가 종료 대기 중 강제 종료")
        self._finish_quit()

    def _finish_quit(self) -> None:
        if self._quit_dialog:
            self._quit_dialog.canceled.disconnect(self._force_quit)
            self._quit_dialog.close()
            self._quit_dialog = None
        self.tray.hide()
        from PySide6.QtWidgets import QApplication

        QApplication.quit()
