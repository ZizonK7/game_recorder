"""게임 감지 -> 녹화 -> 이벤트 수집 -> 종료 후 정리 까지 담당하는 핵심 루프.

백그라운드 스레드에서 1초마다 돌며, UI에는 Qt 시그널로 상태를 알린다.
폴링 간격이 짧고 요청이 가벼워(로컬 HTTP 몇 번) 게임 성능에는 영향이 없다.
"""

from __future__ import annotations

import logging
import os
import shutil
import statistics
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import events as ev
from .config import Settings, save_settings
from .paths import audio_helper_path, ffmpeg_path
from .recorder.audio import GameAudioCapture
from .recorder.ffmpeg import (
    FfmpegRecorder, Pipeline, RecorderConfig, build_replay_clip, concat_segments, detect_pipeline,
    probe_duration, target_size,
)
from .riot.api import match_id_for, normalize_platform
from .riot.local import GameSessionInfo, LcuClient, LiveClient, game_pid, game_window_mode, read_session
from .storage import Storage

log = logging.getLogger(__name__)


def safe_name(text: str) -> str:
    return "".join(c for c in text if c.isalnum() or c in "-_ ").strip() or "game"


class PipelineCache:
    """인코더 자동 탐지 결과를 설정에 저장해 매번 테스트하지 않도록 한다."""

    def __init__(self, settings: Settings, screen_size: tuple[int, int]):
        self.settings = settings
        self.screen_size = screen_size
        self._lock = threading.Lock()

    def key(self) -> str:
        s = self.settings
        return f"{s.encoder}|{s.fps}|{s.bitrate_kbps}|{s.target_height}|{s.monitor_index}|{self.screen_size}"

    def get(self, ffmpeg: Path) -> Pipeline | None:
        with self._lock:
            cached = self.settings.detected_pipeline or {}
            if cached.get("key") == self.key():
                return Pipeline(cached["name"], cached.get("vf"), list(cached["codec_args"]))
            s = self.settings
            size = target_size(*self.screen_size, s.target_height)
            p = detect_pipeline(ffmpeg, s.encoder, s.fps, s.bitrate_kbps, s.monitor_index, size)
            if p:
                s.detected_pipeline = {"key": self.key(), **asdict(p)}
                save_settings(s)
            return p

    def invalidate(self) -> None:
        with self._lock:
            self.settings.detected_pipeline = {}


class GameWatcher(QObject):
    status_changed = Signal(str)           # 상태 표시줄 문구
    recording_started = Signal(int)        # game row id
    recording_finished = Signal(int)       # game row id (영상 준비 완료)
    death_replay_ready = Signal(str, float, float)  # clip path, seek, length
    respawned = Signal()
    error = Signal(str)

    POLL_SEC = 1.0

    def __init__(self, settings: Settings, storage: Storage, pipelines: PipelineCache):
        super().__init__()
        self.settings = settings
        self.storage = storage
        self.pipelines = pipelines
        self.live = LiveClient()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_game_processed = None  # 콜백: 녹화 정리 후 API 수집 요청 (game row id)
        self.recording = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="game-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=180)

    def _run(self) -> None:
        try:
            self.recover_unfinished()
        except Exception:
            log.exception("중단된 녹화 복구 실패")
        # 인코더 탐지를 미리 해두어 게임 시작 시 지연이 없도록 (결과는 설정에 캐시)
        ffmpeg = ffmpeg_path()
        if ffmpeg is not None and os.name == "nt":
            self.status_changed.emit("인코더 확인 중...")
            try:
                p = self.pipelines.get(ffmpeg)
                log.info("사용할 인코더: %s", p.name if p else "없음")
            except Exception:
                log.exception("인코더 탐지 실패")
        self.status_changed.emit("게임 대기 중")
        while not self._stop.is_set():
            try:
                if self._should_start():
                    self._record_game()
                    self.status_changed.emit("게임 대기 중")
            except Exception as e:  # 루프가 죽으면 이후 녹화가 전부 안 되므로 반드시 잡는다
                log.exception("게임 감지 루프 오류")
                self.error.emit(f"오류: {e}")
            self._stop.wait(2.0)

    def _should_start(self) -> bool:
        if game_pid() is None:
            return False
        if not self.live.is_available():
            return False
        lcu = LcuClient.connect()
        if lcu is not None:
            phase = lcu.gameflow_phase()
            # 리플레이/관전은 InProgress 가 아님
            if phase is not None and phase != "InProgress":
                return False
        return True

    # ------------------------------------------------------------------ recording
    def _record_game(self) -> None:
        s = self.settings
        lcu = LcuClient.connect()
        info: GameSessionInfo = read_session(lcu)

        if not s.should_record_queue(info.queue_id):
            self.status_changed.emit(f"녹화 안 함 ({info.queue_name or '랭크 아님'}) - 게임 종료 대기")
            self._wait_game_end()
            return

        ffmpeg = ffmpeg_path()
        if ffmpeg is None:
            self.error.emit("ffmpeg 를 찾을 수 없습니다. 녹화를 할 수 없습니다.")
            self._wait_game_end()
            return
        self.status_changed.emit("인코더 확인 중...")
        pipeline = self.pipelines.get(ffmpeg)
        if pipeline is None:
            self.error.emit("사용 가능한 인코더가 없습니다 (ffmpeg.log 확인)")
            self._wait_game_end()
            return

        if info.puuid and info.puuid != s.puuid:
            s.puuid = info.puuid
            save_settings(s)

        platform = normalize_platform(s.region if s.region != "auto" else info.region)
        me_name = self.live.active_player_name() or info.riot_id or s.riot_id
        players = self.live.player_list()
        who = ev.PlayerIdentity(me_name or "", players)
        champion = next((p.get("championName", "") for p in players if who.is_me(p.get("riotId") or p.get("summonerName"))), "")

        started = datetime.now()
        folder = s.recordings_path / f"{started:%Y-%m-%d_%H%M%S}_{safe_name(champion or 'LoL')}"
        folder.mkdir(parents=True, exist_ok=True)

        game_row = self.storage.create_game(
            match_id=match_id_for(platform, info.game_id) if info.game_id else None,
            game_id=info.game_id, platform=platform, queue_id=info.queue_id, champion=champion,
            riot_id=me_name, puuid=info.puuid or s.puuid, started_at=started.isoformat(timespec="seconds"),
            folder=str(folder), status="recording", api_status="pending",
        )

        # 게임 소리 캡처 (실패해도 영상은 녹화)
        audio: GameAudioCapture | None = None
        helper = audio_helper_path()
        pid = game_pid()
        if s.record_game_audio and helper and pid:
            audio = GameAudioCapture(helper, pid)
            if not audio.start():
                self.error.emit("게임 소리 캡처를 시작하지 못해 영상만 녹화합니다")
                audio = None

        recorder = FfmpegRecorder(
            RecorderConfig(ffmpeg, pipeline, s.monitor_index, s.fps, audio.pipe_name if audio else None),
            folder,
        )
        recorder.start()
        self.recording = True
        self.recording_started.emit(game_row)
        mode = game_window_mode()
        mode_note = " (전체 화면 모드: 데스 리플레이 창이 보이지 않을 수 있음)" if mode == "fullscreen" else ""
        self.status_changed.emit(f"녹화 중 - {champion} [{pipeline.name}]{mode_note}")

        collected: list[ev.GameEvent] = []
        seen_ids: set = set()
        offsets: deque[float] = deque(maxlen=60)
        missing_since: float | None = None
        game_ended_at: float | None = None
        death_count = 0
        dead = False

        try:
            while not self._stop.is_set():
                time.sleep(self.POLL_SEC)
                if not recorder.is_running():
                    self.error.emit("녹화 프로세스가 예기치 않게 종료되었습니다 (ffmpeg.log 확인)")
                    break

                stats = self.live.game_stats()
                vt = recorder.video_time_now()
                if stats is None:
                    missing_since = missing_since or time.monotonic()
                    limit = 3 if game_ended_at else 15
                    if time.monotonic() - missing_since > limit or game_pid() is None:
                        break
                    continue
                missing_since = None
                if vt is not None and "gameTime" in stats:
                    offsets.append(vt - float(stats["gameTime"]))

                if not who.my_team:  # 로딩 직후엔 목록이 비어 있을 수 있음
                    players = self.live.player_list()
                    if players:
                        who = ev.PlayerIdentity(me_name or "", players)

                for raw in self.live.events():
                    eid = raw.get("EventID")
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    gev = ev.from_live_event(raw, who)
                    if gev is None:
                        continue
                    collected.append(gev)
                    # 비정상 종료 대비: 이벤트를 수시로 저장 (offset 은 아직 추정치)
                    ev.save_events(folder / "events.json", collected,
                                   statistics.median(offsets) if offsets else 0.0, {"me": me_name, "partial": True})
                    if gev.type == "game_end":
                        game_ended_at = time.monotonic()
                    if gev.type == "death" and s.death_replay_enabled and offsets:
                        death_count += 1
                        dead = True
                        offset = statistics.median(offsets)
                        self._spawn_replay(ffmpeg, recorder, folder, gev.game_time + offset, death_count)

                if dead and s.replay_close_on_respawn and self._is_alive(who):
                    dead = False
                    self.respawned.emit()

                if game_ended_at and time.monotonic() - game_ended_at > 8:
                    break
        finally:
            recorder.stop()
            if audio:
                audio.stop()
            self.recording = False

        offset = statistics.median(offsets) if offsets else 0.0
        self.status_changed.emit("영상 정리 중...")
        self.storage.update_game(game_row, status="processing")
        args = (game_row, ffmpeg, recorder.seg_dir, folder, collected, offset, me_name)
        if self._stop.is_set():
            # 앱 종료 중이면 정리가 끝날 때까지 기다린다
            self._finalize(*args)
            return
        threading.Thread(target=self._finalize, args=args, daemon=True, name="finalize").start()
        self._wait_game_end()

    def _is_alive(self, who: ev.PlayerIdentity) -> bool:
        for p in self.live.player_list():
            if who.is_me(p.get("riotId") or p.get("summonerName")):
                return not p.get("isDead", False)
        return False

    def _wait_game_end(self) -> None:
        while not self._stop.is_set() and game_pid() is not None:
            time.sleep(2)

    # ------------------------------------------------------------------ death replay
    def _spawn_replay(self, ffmpeg: Path, recorder: FfmpegRecorder, folder: Path, death_vt: float, n: int) -> None:
        s = self.settings

        def work():
            target_end = death_vt + s.replay_after_sec
            deadline = time.monotonic() + 15
            segments = recorder.segments()
            while (not segments or segments[-1].end < target_end) and time.monotonic() < deadline:
                time.sleep(0.25)
                segments = recorder.segments()
            out = folder / "deaths" / f"death_{n:02d}.mp4"
            out.parent.mkdir(exist_ok=True)
            clip = build_replay_clip(ffmpeg, segments, death_vt, s.replay_before_sec, s.replay_after_sec, out)
            if clip:
                self.death_replay_ready.emit(str(clip.path), clip.seek, clip.length)
            else:
                log.warning("데스 리플레이 생성 실패")

        threading.Thread(target=work, daemon=True, name="death-replay").start()

    # ------------------------------------------------------------------ finalize
    def _finalize(self, game_row: int, ffmpeg: Path, seg_dir: Path, folder: Path,
                  collected: list[ev.GameEvent], offset: float, me_name: str | None) -> None:
        try:
            video = folder / "video.mp4"
            segs = sorted(seg_dir.glob("seg_*.ts"))
            ok = concat_segments(ffmpeg, segs, video)
            duration = probe_duration(ffmpeg, video) if ok else None
            visible = []
            for e in collected:
                vt = e.game_time + offset
                if vt < 0 or (duration and vt > duration + 1):
                    continue
                e.video_time = min(vt, duration) if duration else vt
                visible.append(e)
            ev.save_events(folder / "events.json", visible, offset, {"me": me_name})
            if ok:
                shutil.rmtree(seg_dir, ignore_errors=True)
                shutil.rmtree(folder / "deaths", ignore_errors=True)
                self.storage.update_game(game_row, status="ready", video_path=str(video), duration_sec=duration)
            else:
                # 합치기에 실패하면 조각은 남겨두어 데이터 손실을 막는다
                self.storage.update_game(game_row, status="failed", note="조각 합치기 실패 - segments 폴더 확인")
            self.recording_finished.emit(game_row)
            self.status_changed.emit("녹화 저장 완료")
            enforce_storage_limit(self.storage, self.settings.max_storage_gb)
        except Exception as e:
            log.exception("녹화 정리 실패")
            self.storage.update_game(game_row, status="failed", note=str(e))
            self.error.emit(f"녹화 정리 실패: {e}")
        if self.on_game_processed:
            self.on_game_processed(game_row)

    def recover_unfinished(self) -> None:
        """이전 실행이 녹화/정리 도중 끊겼다면 남은 조각으로 영상을 복구."""
        ffmpeg = ffmpeg_path()
        for g in self.storage.list_games():
            if g.status not in ("recording", "processing") or not g.folder:
                continue
            folder = Path(g.folder)
            seg_dir = folder / "segments"
            if ffmpeg is None or not seg_dir.exists():
                self.storage.update_game(g.id, status="failed", note="녹화가 중단되었습니다")
                continue
            log.info("중단된 녹화 복구: %s", folder)
            collected, offset, meta = ev.load_events(folder / "events.json")
            self._finalize(g.id, ffmpeg, seg_dir, folder, collected, offset, meta.get("me"))


def enforce_storage_limit(storage: Storage, max_gb: float) -> list[int]:
    """녹화 폴더 전체가 용량 제한을 넘으면 오래된 영상부터 삭제 (경기 데이터는 유지)."""
    if max_gb <= 0:
        return []
    games = [g for g in storage.list_games() if g.has_video]
    sizes = {g.id: Path(g.video_path).stat().st_size for g in games}
    total = sum(sizes.values())
    limit = max_gb * 1024 ** 3
    removed = []
    for g in sorted(games, key=lambda g: g.started_at or ""):
        if total <= limit:
            break
        try:
            Path(g.video_path).unlink()
        except OSError:
            continue
        total -= sizes[g.id]
        storage.update_game(g.id, video_path=None, status="none", note="용량 제한으로 영상 자동 삭제")
        removed.append(g.id)
    return removed
