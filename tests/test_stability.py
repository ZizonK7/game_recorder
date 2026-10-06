"""검토에서 지적된 녹화 재개 / 리플레이 경쟁 / 복구 문제 회귀 테스트."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest
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


def test_storage_limit_counts_all_files_and_keeps_failed(tmp_path):
    st = Storage(tmp_path / "db.sqlite", tmp_path / "data")
    mb = 1024 ** 2

    def game(name, status, video_mb=0, seg_mb=0):
        f = tmp_path / name
        f.mkdir()
        video = None
        if video_mb:
            video = f / "video.mp4"
            video.write_bytes(b"0" * video_mb * mb)
        if seg_mb:
            (f / "segments").mkdir()
            (f / "segments" / "seg_00000.ts").write_bytes(b"0" * seg_mb * mb)
        return st.create_game(folder=str(f), status=status, started_at=name,
                              video_path=str(video) if video else None)

    old = game("2026-01", "ready", video_mb=3)
    failed = game("2026-02", "failed", seg_mb=3)
    new = game("2026-03", "ready", video_mb=3)
    # 영상만 세면 6MB 라 8MB 제한 안이지만, 실패한 조각까지 9MB 라 가장 오래된 영상을 지워야 한다
    removed, over = watcher_mod.enforce_storage_limit(st, 8 / 1024, reserve_bytes=0)
    assert removed == [old] and not over
    assert st.get_game(old).status == "none" and st.get_game(new).has_video
    assert (tmp_path / "2026-02" / "segments" / "seg_00000.ts").exists()
    # 곧 녹화할 분량까지 고려하면 남은 영상도 지우고, 그래도 넘으면 알려준다
    removed, over = watcher_mod.enforce_storage_limit(st, 8 / 1024, reserve_bytes=6 * mb)
    assert removed == [new] and over
    assert st.get_game(failed).status == "failed"


def test_stop_waits_for_workers(tmp_path):
    w = _watcher(tmp_path)
    done = []
    w._start_worker(lambda: (time.sleep(0.3), done.append(1)), name="finalize")
    assert w.busy
    w.stop()
    assert done == [1] and not w.busy


def test_finalize_without_space_keeps_segments_and_retries(tmp_path, monkeypatch):
    w = _watcher(tmp_path)
    folder = tmp_path / "game"
    seg_dir = folder / "segments"
    seg_dir.mkdir(parents=True)
    (seg_dir / "seg_00000.ts").write_bytes(b"0" * 1024)
    gid = w.storage.create_game(folder=str(folder), status="processing", match_id="KR_1")
    processed = []
    w.on_game_processed = processed.append
    monkeypatch.setattr(watcher_mod, "free_bytes", lambda p: 10)
    monkeypatch.setattr(watcher_mod, "concat_segments", lambda *a, **k: pytest.fail("합치면 안 됨"))
    events = [ev.GameEvent(type="kill", game_time=5.0, label="킬")]
    w._finalize(gid, Path("ffmpeg"), seg_dir, folder, events, 2.0, "나")
    g = w.storage.get_game(gid)
    assert g.status == "failed" and g.note.startswith(watcher_mod.NOTE_NO_SPACE)
    assert processed == [gid]  # API 데이터 수집은 그대로 진행
    assert (seg_dir / "seg_00000.ts").exists()

    # 공간을 확보한 뒤 다시 켜면 합치기를 다시 시도한다
    monkeypatch.setattr(watcher_mod, "free_bytes", lambda p: 10 ** 12)
    monkeypatch.setattr(watcher_mod, "ffmpeg_path", lambda: Path("ffmpeg"))

    def fake_concat(ffmpeg, segs, out, timeout=600):
        out.write_bytes(b"mp4")
        return True

    monkeypatch.setattr(watcher_mod, "concat_segments", fake_concat)
    monkeypatch.setattr(watcher_mod, "probe_duration", lambda ffmpeg, video: 60.0)
    w.recover_unfinished()
    g = w.storage.get_game(gid)
    assert g.status == "ready" and g.has_video and g.note is None
    loaded, offset, _ = ev.load_events(folder / "events.json")
    assert offset == 2.0 and loaded[0].video_time == 7.0


def test_keep_recent_videos(tmp_path):
    st = Storage(tmp_path / "db.sqlite", tmp_path / "data")
    ids = []
    for day in range(1, 8):
        f = tmp_path / f"g{day}"
        f.mkdir()
        (f / "video.mp4").write_bytes(b"v")
        ids.append(st.create_game(folder=str(f), status="ready", video_path=str(f / "video.mp4"),
                                  started_at=f"2026-10-0{day}T20:00:00"))
    failed = st.create_game(folder=str(tmp_path / "g0"), status="failed", started_at="2026-09-01")
    assert watcher_mod.enforce_keep_recent(st, 0) == []
    removed = watcher_mod.enforce_keep_recent(st, 5)
    assert removed == [ids[0], ids[1]]  # 가장 오래된 2경기 (오래된 것부터)
    assert all(st.get_game(i).has_video for i in ids[2:])
    assert not (tmp_path / "g1" / "video.mp4").exists()
    g = st.get_game(ids[0])
    assert g.status == "none" and "최근 5경기" in g.note
    assert st.get_game(failed).status == "failed"
    assert watcher_mod.enforce_keep_recent(st, 5) == []


def test_keep_recent_counts_matches_not_sessions(tmp_path):
    st = Storage(tmp_path / "db.sqlite", tmp_path / "data")

    def video(name, match_id, session, started):
        f = tmp_path / name
        f.mkdir()
        (f / "video.mp4").write_bytes(b"v")
        return st.create_game(folder=str(f), status="ready", video_path=str(f / "video.mp4"),
                              match_id=match_id, session=session, started_at=started)

    a = video("a", "KR_A", 1, "2026-10-01T20:00:00")
    b1 = video("b1", "KR_B", 1, "2026-10-02T20:00:00")
    b2 = video("b2", "KR_B", 2, "2026-10-02T20:10:00")
    c = video("c", None, 1, "2026-09-01T20:00:00")  # match_id 없는 녹화는 각각 한 경기
    assert watcher_mod.plan_keep_recent(st, 3) == []
    assert [g.id for g in watcher_mod.plan_keep_recent(st, 2)] == [c]
    assert [g.id for g in watcher_mod.plan_keep_recent(st, 1)] == [c, a]
    assert watcher_mod.enforce_keep_recent(st, 1) == [c, a]
    assert st.get_game(b1).has_video and st.get_game(b2).has_video


def test_api_wait_is_cancelled_immediately():
    from lolrec.riot.api import RequestCancelled, RiotApi

    cancel = threading.Event()
    api = RiotApi("key", cancel=cancel)
    api.limiter.block_for(100)  # 429 를 받아 100초 기다리는 중
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(RequestCancelled):
        api.match("KR_1")
    assert time.monotonic() - started < 2


def test_fetcher_stop_waits_until_worker_exits(tmp_path, monkeypatch):
    from lolrec.fetcher import MatchFetcher
    from lolrec.riot.api import RiotApi

    st = Storage(tmp_path / "db.sqlite", tmp_path / "data")
    st.create_game(match_id="KR_1", status="none", api_status="pending")
    f = MatchFetcher(Settings(recordings_dir=str(tmp_path / "rec")), st)
    entered = threading.Event()

    def blocked_api():
        api = RiotApi("key", cancel=f._stop)
        api.limiter.block_for(100)
        entered.set()
        return api

    monkeypatch.setattr(f, "api", blocked_api)
    f.start()
    assert entered.wait(5)
    started = time.monotonic()
    assert f.stop() is True
    assert time.monotonic() - started < 3 and not f._thread.is_alive()
    st.close()  # 작업자가 끝난 뒤라 안전


def test_fetcher_stop_reports_timeout(tmp_path, monkeypatch):
    from lolrec.fetcher import MatchFetcher

    st = Storage(tmp_path / "db.sqlite", tmp_path / "data")
    st.create_game(match_id="KR_1", status="none", api_status="pending")
    f = MatchFetcher(Settings(recordings_dir=str(tmp_path / "rec")), st)
    busy = threading.Event()
    monkeypatch.setattr(f, "_fetch_game", lambda gid, attempt: (busy.set(), time.sleep(1.5)) and None)
    f.start()
    assert busy.wait(5)
    assert f.stop(timeout=0.2) is False  # 끝나지 않았으면 정상 종료로 보지 않는다
    assert f.stop() is True
