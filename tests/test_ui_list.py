"""녹화 목록 갱신 / 삭제 / 원본 데이터 위치 회귀 테스트 (offscreen Qt)."""

import pytest
from PySide6.QtWidgets import QMessageBox

from lolrec.config import Settings
from lolrec.fetcher import MatchFetcher
from lolrec.storage import Storage
from lolrec.ui import main_window as mw
from lolrec.watcher import GameWatcher

from .fakes import make_match, make_timeline


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(GameWatcher, "start", lambda self: None)
    monkeypatch.setattr(MatchFetcher, "start", lambda self: None)
    monkeypatch.setattr(mw, "save_settings", lambda s: None)
    monkeypatch.setattr(mw, "set_autostart", lambda enabled: None)  # 실제 레지스트리를 건드리지 않게
    st = Storage(tmp_path / "db.sqlite", tmp_path / "rec" / "data")
    s = Settings(recordings_dir=str(tmp_path / "rec"))
    w = mw.MainWindow(s, st, (1920, 1080))
    calls = {"load": 0, "reload": 0}
    orig_load, orig_reload = w.player.load, w.player.reload_events

    def load(*a):
        calls["load"] += 1
        orig_load(*a)

    def reload(*a):
        calls["reload"] += 1
        orig_reload(*a)

    monkeypatch.setattr(w.player, "load", load)
    monkeypatch.setattr(w.player, "reload_events", reload)
    w.calls = calls
    yield w
    w._quitting = True
    w.tray.hide()
    w.close()
    st.close()


def _add(st, mid, started):
    return st.create_game(match_id=mid, started_at=started, status="none", api_status="pending")


def test_refresh_keeps_multi_selection_without_reloading(window):
    st = window.storage
    a, b = _add(st, "KR_1", "2026-10-01"), _add(st, "KR_2", "2026-10-02")
    _add(st, "KR_3", "2026-10-03")
    window.refresh_list()
    window.table.selectRow(1)
    assert window.calls["load"] == 1
    window.table.selectAll()
    loads = window.calls["load"]
    window.refresh_list()
    assert len(window._selected_ids()) == 3
    assert window.calls["load"] == loads

    # 하나만 선택된 상태에서 데이터가 갱신되면 영상은 다시 열지 않고 이벤트만 다시 읽는다
    window.table.clearSelection()
    window.table.selectRow(0)  # 최신 경기 (KR_3)
    loads = window.calls["load"]
    gid = window._selected_ids()[0]
    st.update_game(gid, kills=1, deaths=2, assists=3)
    window._on_game_updated(gid)
    assert window.calls["load"] == loads and window.calls["reload"] == 1
    assert window._selected_ids() == [gid]
    # 다른 경기가 갱신되면 아무것도 다시 읽지 않는다
    window._on_game_updated(a)
    window._on_game_updated(b)
    assert window.calls["load"] == loads and window.calls["reload"] == 1


def test_full_delete_removes_raw_json(window, monkeypatch):
    st = window.storage
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    gid = _add(st, "KR_1", "2026-10-01")
    st.save_raw("KR_1", make_match("KR_1"), make_timeline(minutes=2))
    # 같은 경기를 이어서 녹화한 행이 남아 있으면 원본은 지우지 않는다
    gid2 = st.create_game(match_id="KR_1", session=2, status="none")
    window._delete([st.get_game(gid)], keep_data=False)
    assert st.match_json_path("KR_1").exists()
    window._delete([st.get_game(gid2)], keep_data=False)
    assert not st.match_json_path("KR_1").exists()
    assert not st.timeline_json_path("KR_1").exists()
    assert st.list_games() == []


def test_relocate_and_import_raw_data(tmp_path):
    old, new = tmp_path / "old" / "data", tmp_path / "new" / "data"
    st = Storage(tmp_path / "db.sqlite", old)
    st.save_raw("KR_1", make_match("KR_1"), make_timeline(minutes=2))
    assert st.relocate_data(new) == 2
    assert st.load_match("KR_1") is not None
    assert not (old / "matches" / "KR_1.json").exists()

    # 예전 버전처럼 경로만 바뀐 채 재시작한 경우: 남은 파일을 가져온다
    st2 = Storage(tmp_path / "db2.sqlite", tmp_path / "newer" / "data")
    assert st2.load_match("KR_1") is None
    assert st2.import_data_from(new) == 2
    assert st2.load_match("KR_1") is not None and st2.load_timeline("KR_1") is not None
    assert st2.import_data_from(st2.data_dir) == 0


def test_quit_does_not_block_ui_while_finalizing(window, monkeypatch, qapp):
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    release = threading.Event()
    monkeypatch.setattr(type(window.watcher), "busy", property(lambda self: True))
    monkeypatch.setattr(window.watcher, "stop", lambda: release.wait(5))
    monkeypatch.setattr(window.fetcher, "stop", lambda: None)
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(1)))

    started = time.monotonic()
    window.quit()
    assert time.monotonic() - started < 1  # 정리를 기다리느라 UI 스레드가 멈추지 않는다
    assert window._quit_dialog is not None and quits == []

    release.set()
    end = time.monotonic() + 3
    while not quits and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.02)
    assert quits == [1] and window._quit_dialog is None



def _videos(st, tmp_path, n):
    ids = []
    for day in range(1, n + 1):
        f = tmp_path / f"g{day}"
        f.mkdir()
        (f / "video.mp4").write_bytes(b"v" * 1024)
        ids.append(st.create_game(folder=str(f), status="ready", video_path=str(f / "video.mp4"),
                                  started_at=f"2026-10-0{day}T20:00:00"))
    return ids


class _FakeDialog:
    """설정 창 대신: 받은 설정(사본)을 바꾸고 저장 버튼을 누른 것처럼 동작."""
    changes: dict = {}

    def __init__(self, settings, parent=None):
        self.settings = settings

    def exec(self):
        for k, v in self.changes.items():
            setattr(self.settings, k, v)
        return True


def test_declining_cleanup_never_exposes_new_value(window, monkeypatch, tmp_path):
    from lolrec.watcher import enforce_keep_recent

    st = window.storage
    _videos(st, tmp_path, 3)
    _FakeDialog.changes = {"keep_recent_videos": 1, "replay_volume": 0.5}
    monkeypatch.setattr(mw, "SettingsDialog", _FakeDialog)
    seen = []

    def question(*a, **k):
        # 확인 창이 떠 있는 동안 백그라운드 영상 정리가 실행되는 상황
        seen.append(window.settings.keep_recent_videos)
        enforce_keep_recent(st, window.settings.keep_recent_videos)
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", question)
    window._open_settings()
    assert seen == [0]  # 확인 전에는 새 값이 공개되지 않는다
    assert sum(g.has_video for g in st.list_games()) == 3
    assert window.settings.keep_recent_videos == 0
    assert window.settings.replay_volume == 0.5  # 나머지 설정은 저장

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window._open_settings()
    assert window.settings.keep_recent_videos == 1
    assert [g.has_video for g in st.list_games()] == [True, False, False]


def test_lowering_storage_limit_also_asks(window, monkeypatch, tmp_path):
    st = window.storage
    _videos(st, tmp_path, 3)
    _FakeDialog.changes = {"max_storage_gb": 2.5 * 1024 / 1024 ** 3}  # 영상 2.5개 분량
    monkeypatch.setattr(mw, "SettingsDialog", _FakeDialog)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    window._open_settings()
    assert window.settings.max_storage_gb == 100.0
    assert sum(g.has_video for g in st.list_games()) == 3


def test_settings_dialog_edits_copy_and_keeps_concurrent_changes(window, monkeypatch):
    _FakeDialog.changes = {"fps": 60}
    opened = []

    class Dialog(_FakeDialog):
        def exec(self):
            opened.append(self.settings is window.settings)
            window.settings.puuid = "found-meanwhile"  # 창이 열린 동안 다른 작업이 바꾼 값
            return super().exec()

    monkeypatch.setattr(mw, "SettingsDialog", Dialog)
    window._open_settings()
    assert opened == [False]
    assert window.settings.fps == 60 and window.settings.puuid == "found-meanwhile"
