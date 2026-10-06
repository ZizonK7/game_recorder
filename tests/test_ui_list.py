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


def test_keep_recent_setting_asks_before_deleting(window, monkeypatch, tmp_path):
    st = window.storage
    for day in range(1, 4):
        f = tmp_path / f"g{day}"
        f.mkdir()
        (f / "video.mp4").write_bytes(b"v")
        st.create_game(folder=str(f), status="ready", video_path=str(f / "video.mp4"),
                       started_at=f"2026-10-0{day}T20:00:00")
    window.settings.keep_recent_videos = 1
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    assert not window._apply_keep_recent()
    assert sum(g.has_video for g in st.list_games()) == 3
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    assert window._apply_keep_recent()
    assert [g.has_video for g in st.list_games()] == [True, False, False]
