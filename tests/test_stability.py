"""검토에서 지적된 녹화 재개 / 리플레이 경쟁 / 복구 문제 회귀 테스트."""

import sqlite3
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from lolrec import events as ev
from lolrec import watcher as watcher_mod
from lolrec.config import Settings
from lolrec.recorder.ffmpeg import ReplayClip, Segment
from lolrec.storage import Storage
from lolrec.watcher import GameWatcher, PipelineCache


def test_same_match_gets_new_session(tmp_path):
    st = Storage(tmp_path / "db.sqlite", tmp_path / "data")
    assert st.next_session("KR_1") == 1
    first = st.create_game(match_id="KR_1", session=1, status="ready")
    assert st.next_session("KR_1") == 2
    second = st.create_game(match_id="KR_1", session=2, status="recording")
    assert first != second
    assert st.get_game(second).session == 2
    assert st.find_by_match_id("KR_1").id == first
    # match_id 가 없는 녹화는 여러 개여도 된다
    st.create_game(match_id=None, status="ready")
    st.create_game(match_id=None, status="ready")


def test_migrates_old_unique_schema(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT UNIQUE, game_id INTEGER, platform TEXT,
            queue_id INTEGER, champion TEXT, riot_id TEXT, puuid TEXT, started_at TEXT, duration_sec REAL,
            folder TEXT, video_path TEXT, status TEXT DEFAULT 'none', api_status TEXT DEFAULT 'pending',
            win INTEGER, kills INTEGER, deaths INTEGER, assists INTEGER, note TEXT);
        CREATE INDEX idx_games_started ON games(started_at);
        INSERT INTO games (match_id, champion, status) VALUES ('KR_9', 'Ahri', 'ready');
    """)
    conn.commit()
    conn.close()

    st = Storage(db, tmp_path / "data")
    g = st.find_by_match_id("KR_9")
    assert g.champion == "Ahri" and g.session == 1
    st.create_game(match_id="KR_9", session=st.next_session("KR_9"), status="recording")
    assert st.next_session("KR_9") == 3
    st.close()
    Storage(db, tmp_path / "data").close()  # 두 번째 실행에서는 변환하지 않음


def test_events_save_is_atomic_and_load_tolerates_corruption(tmp_path):
    p = tmp_path / "events.json"
    e = ev.GameEvent(type="kill", game_time=10.0, label="킬")
    ev.save_events(p, [e], 1.5, {"me": "나"})
    assert not p.with_name("events.json.tmp").exists()
    loaded, offset, meta = ev.load_events(p)
    assert len(loaded) == 1 and offset == 1.5 and meta["me"] == "나"

    p.write_text('{"offset": 1.0, "events": [{"type": "ki', encoding="utf-8")  # 쓰다가 잘린 파일
    assert ev.load_events(p) == ([], 0.0, {})


def _watcher(tmp_path) -> GameWatcher:
    st = Storage(tmp_path / "db.sqlite", tmp_path / "data")
    s = Settings(recordings_dir=str(tmp_path / "rec"), replay_after_sec=0.0)
    return GameWatcher(s, st, PipelineCache(s, (1920, 1080)))


class _FakeRecorder:
    def segments(self):
        return [Segment(Path("seg_00000.ts"), 0.0, 100.0)]


def _pump(seconds: float, until=lambda: False) -> None:
    """다른 스레드에서 보낸 시그널은 Qt 이벤트로 전달되므로 직접 처리해 준다."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        QCoreApplication.processEvents()
        time.sleep(0.02)


def test_late_replay_is_dropped_after_respawn(qapp, tmp_path, monkeypatch):
    w = _watcher(tmp_path)
    started, release = threading.Event(), threading.Event()

    def slow_clip(ffmpeg, segments, death_vt, before, after, out):
        started.set()
        release.wait(5)
        return ReplayClip(out, 0.0, 5.0)

    monkeypatch.setattr(watcher_mod, "build_replay_clip", slow_clip)
    shown, closed = [], []
    w.death_replay_ready.connect(lambda *a: shown.append(a))
    w.respawned.connect(lambda: closed.append(True))

    w._spawn_replay(Path("ffmpeg"), _FakeRecorder(), tmp_path, 50.0, 1)
    assert started.wait(5)
    w._cancel_replays()  # 클립을 만드는 동안 부활
    release.set()
    _pump(0.5)
    assert shown == [] and closed == [True]

    # 부활 전에 완성되면 정상적으로 띄운다
    release.clear()
    started.clear()
    w._spawn_replay(Path("ffmpeg"), _FakeRecorder(), tmp_path, 60.0, 2)
    assert started.wait(5)
    release.set()
    _pump(3, lambda: bool(shown))
    assert len(shown) == 1


def test_recovery_continues_after_one_game_fails(tmp_path, monkeypatch):
    w = _watcher(tmp_path)
    folders = []
    for name in ("a", "b"):
        f = tmp_path / name
        (f / "segments").mkdir(parents=True)
        folders.append(f)
        w.storage.create_game(folder=str(f), status="recording", started_at=name)
    monkeypatch.setattr(watcher_mod, "ffmpeg_path", lambda: Path("ffmpeg"))

    def finalize(gid, *args):
        if w.storage.get_game(gid).folder == str(folders[0]):
            raise RuntimeError("boom")
        w.storage.update_game(gid, status="ready")

    monkeypatch.setattr(w, "_finalize", finalize)
    w.recover_unfinished()
    statuses = {g.folder: g.status for g in w.storage.list_games()}
    assert statuses == {str(folders[0]): "failed", str(folders[1]): "ready"}


def test_screen_size_change_invalidates_pipeline_cache(tmp_path):
    s = Settings(recordings_dir=str(tmp_path / "rec"))
    cache = PipelineCache(s, (1920, 1080))
    s.detected_pipeline = {"key": cache.key(), "name": "x264", "vf": None, "codec_args": []}
    assert not cache.set_screen_size((1920, 1080))
    assert s.detected_pipeline
    assert cache.set_screen_size((3440, 1440))
    assert s.detected_pipeline == {} and "(3440, 1440)" in cache.key()
