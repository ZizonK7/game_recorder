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
from .storage import GameRow, Storage

log = logging.getLogger(__name__)


GB = 1024 ** 3
# 녹화 중 남은 공간이 이보다 적으면 녹화를 멈춘다 (지금까지 녹화한 부분은 저장)
STOP_FREE_BYTES = 1 * GB
# 한 판을 녹화할 때 예상하는 최대 길이 (용량 확보 / 공간 경고 기준)
EXPECTED_GAME_SEC = 45 * 60
# 공간 부족으로 합치지 못한 녹화의 note 접두어. 다음 실행 때 다시 합치기를 시도한다.
NOTE_NO_SPACE = "디스크 공간 부족으로 영상 합치기 못함"


def safe_name(text: str) -> str:
    return "".join(c for c in text if c.isalnum() or c in "-_ ").strip() or "game"


def free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def expected_game_bytes(bitrate_kbps: int) -> int:
    # 영상 + 오디오(160k), 합칠 때는 조각과 결과 mp4 가 잠시 같이 있으므로 호출하는 쪽에서 2배로 본다
    return int((bitrate_kbps + 160) * 1000 / 8 * EXPECTED_GAME_SEC)


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

    def set_screen_size(self, size: tuple[int, int]) -> bool:
        """녹화할 모니터의 실제 해상도가 바뀌면 반영하고 인코더를 다시 탐지하게 한다."""
        with self._lock:
            if tuple(size) == tuple(self.screen_size):
                return False
            log.info("녹화 모니터 해상도 변경: %s -> %s", self.screen_size, size)
            self.screen_size = tuple(size)
            self.settings.detected_pipeline = {}
            return True


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
        # 녹화가 끝난 뒤 도는 작업(영상 정리, 데스 리플레이). 앱 종료 시 끝날 때까지 기다린다.
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
        # 데스 리플레이 세대: 부활/게임 종료 때 올려서, 늦게 완성된 리플레이가 뜨지 않게 한다.
        # 확인과 시그널 발생을 같은 락 안에서 해야 respawned 보다 늦게 재생 시그널이 도착하지 않는다.
        self._replay_gen = 0
        self._replay_lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="game-watcher")
        self._thread.start()

    def stop(self) -> None:
        """감지 루프와 남은 작업(영상 정리 등)이 끝날 때까지 기다린다. UI 스레드에서 부르지 말 것."""
        self._stop.set()
        if self._thread:
            self._thread.join()
        while True:
            with self._workers_lock:
                workers = list(self._workers)
            if not workers:
                break
            for t in workers:
                t.join()

    @property
    def busy(self) -> bool:
        """녹화 중이거나 영상 정리가 남아 있는지."""
        with self._workers_lock:
            finalizing = any(t.name == "finalize" for t in self._workers)
        return self.recording or finalizing

    def _start_worker(self, target, args: tuple = (), name: str = "worker") -> None:
        def run():
            try:
                target(*args)
            finally:
                with self._workers_lock:
                    self._workers.discard(threading.current_thread())

        t = threading.Thread(target=run, daemon=True, name=name)
        with self._workers_lock:
            self._workers.add(t)
        t.start()

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

        self._prepare_space()

        started = datetime.now()
        folder = s.recordings_path / f"{started:%Y-%m-%d_%H%M%S}_{safe_name(champion or 'LoL')}"
        folder.mkdir(parents=True, exist_ok=True)

        # 경기 도중 앱을 다시 켜면 같은 경기를 별도 녹화(session 2, 3 ...)로 이어서 기록한다
        match_id = match_id_for(platform, info.game_id) if info.game_id else None
        session = self.storage.next_session(match_id)
        if session > 1:
            log.info("이미 기록된 경기에 다시 들어옴: %s (녹화 %d)", match_id, session)
        game_row = self.storage.create_game(
            match_id=match_id, session=session,
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
        last_space_check = time.monotonic()

        try:
            while not self._stop.is_set():
                time.sleep(self.POLL_SEC)
                if not recorder.is_running():
                    self.error.emit("녹화 프로세스가 예기치 않게 종료되었습니다 (ffmpeg.log 확인)")
                    break
                if time.monotonic() - last_space_check > 15:
                    last_space_check = time.monotonic()
                    free = free_bytes(folder)
                    if free is not None and free < STOP_FREE_BYTES:
                        self.error.emit("디스크 공간이 부족해 녹화를 중지했습니다. 지금까지 녹화한 부분은 저장합니다.")
                        self.storage.update_game(game_row, note="디스크 공간 부족으로 녹화 중지")
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
                    # 비정상 종료 대비: 이벤트를 수시로 저장 (offset 은 아직 추정치). 실패해도 녹화는 계속
                    try:
                        ev.save_events(folder / "events.json", collected,
                                       statistics.median(offsets) if offsets else 0.0,
                                       {"me": me_name, "partial": True})
                    except OSError:
                        log.warning("녹화 중 이벤트 저장 실패", exc_info=True)
                    if gev.type == "game_end":
                        game_ended_at = time.monotonic()
                    if gev.type == "death" and s.death_replay_enabled and offsets:
                        death_count += 1
                        dead = True
                        offset = statistics.median(offsets)
                        self._spawn_replay(ffmpeg, recorder, folder, gev.game_time + offset, death_count)

                if dead and self._is_alive(who):
                    dead = False
                    if s.replay_close_on_respawn:
                        self._cancel_replays()

                if game_ended_at and time.monotonic() - game_ended_at > 8:
                    break
        finally:
            self._cancel_replays()  # 게임이 끝나면 만들던 리플레이도 띄우지 않는다
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
        self._start_worker(self._finalize, args, name="finalize")
        self._wait_game_end()

    def _prepare_space(self) -> None:
        """녹화 시작 전에 용량 제한을 맞추고, 남은 공간이 부족하면 미리 알린다."""
        s = self.settings
        need = expected_game_bytes(s.bitrate_kbps)
        try:
            removed, over = enforce_storage_limit(self.storage, s.max_storage_gb, reserve_bytes=need)
            if over:
                self.error.emit("녹화 폴더가 용량 제한을 넘었지만 자동으로 지울 영상이 없습니다 "
                                "(실패한 녹화의 segments 폴더 등을 확인하세요)")
        except Exception:
            log.exception("용량 제한 적용 실패")
        free = free_bytes(s.recordings_path)
        if free is not None and free < need * 2:
            self.error.emit(f"디스크 남은 공간이 {free / GB:.1f} GB 입니다. "
                            "긴 게임은 녹화나 영상 정리가 중간에 멈출 수 있습니다.")

    def _is_alive(self, who: ev.PlayerIdentity) -> bool:
        for p in self.live.player_list():
            if who.is_me(p.get("riotId") or p.get("summonerName")):
                return not p.get("isDead", False)
        return False

    def _wait_game_end(self) -> None:
        while not self._stop.is_set() and game_pid() is not None:
            time.sleep(2)

    # ------------------------------------------------------------------ death replay
    def _cancel_replays(self) -> None:
        """진행 중인 리플레이 작업을 무효화하고 떠 있는 리플레이 창을 닫는다."""
        with self._replay_lock:
            self._replay_gen += 1
            self.respawned.emit()

    def _spawn_replay(self, ffmpeg: Path, recorder: FfmpegRecorder, folder: Path, death_vt: float, n: int) -> None:
        s = self.settings
        with self._replay_lock:
            gen = self._replay_gen

        def stale() -> bool:
            return self._stop.is_set() or self._replay_gen != gen

        def work():
            target_end = death_vt + s.replay_after_sec
            deadline = time.monotonic() + 15
            segments = recorder.segments()
            while (not segments or segments[-1].end < target_end) and time.monotonic() < deadline:
                if stale():
                    return
                time.sleep(0.25)
                segments = recorder.segments()
            if stale():
                return
            out = folder / "deaths" / f"death_{n:02d}.mp4"
            out.parent.mkdir(exist_ok=True)
            clip = build_replay_clip(ffmpeg, segments, death_vt, s.replay_before_sec, s.replay_after_sec, out)
            if not clip:
                log.warning("데스 리플레이 생성 실패")
                return
            with self._replay_lock:
                if stale():
                    log.info("이미 부활했거나 게임이 끝나 데스 리플레이를 띄우지 않음")
                    return
                self.death_replay_ready.emit(str(clip.path), clip.seek, clip.length)

        self._start_worker(work, name="death-replay")

    # ------------------------------------------------------------------ finalize
    def _finalize(self, game_row: int, ffmpeg: Path, seg_dir: Path, folder: Path,
                  collected: list[ev.GameEvent], offset: float, me_name: str | None) -> None:
        try:
            video = folder / "video.mp4"
            segs = sorted(seg_dir.glob("seg_*.ts"))
            # 합치는 동안 조각과 mp4 가 같이 있으므로 조각 크기만큼 여유가 있어야 한다
            seg_bytes = sum(p.stat().st_size for p in segs if p.exists())
            free = free_bytes(folder)
            if free is not None and free < seg_bytes + 256 * 1024 ** 2:
                # 조각과 원래 이벤트(게임 시간 기준)를 그대로 두고, 다음 실행 때 다시 합치기를 시도한다
                ev.save_events(folder / "events.json", collected, offset, {"me": me_name, "partial": True})
                self.storage.update_game(game_row, status="failed",
                                         note=f"{NOTE_NO_SPACE} ({seg_bytes / GB:.1f} GB 필요) - "
                                              "공간을 확보한 뒤 프로그램을 다시 켜면 segments 폴더에서 다시 합칩니다")
                self.error.emit("디스크 공간이 부족해 영상을 합치지 못했습니다. 녹화 조각은 보존했습니다.")
                self.recording_finished.emit(game_row)
            else:
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
                    fields = {"status": "ready", "video_path": str(video), "duration_sec": duration}
                    prev = self.storage.get_game(game_row)
                    if prev and (prev.note or "").startswith(NOTE_NO_SPACE):
                        fields["note"] = None  # 공간 부족으로 미뤘던 합치기가 이번에 성공
                    self.storage.update_game(game_row, **fields)
                else:
                    # 합치기에 실패하면 조각은 남겨두어 데이터 손실을 막는다
                    self.storage.update_game(game_row, status="failed", note="조각 합치기 실패 - segments 폴더 확인")
                # 목록이 갱신되기 전에 자동 삭제를 끝내 둔다
                enforce_keep_recent(self.storage, self.settings.keep_recent_videos)
                enforce_storage_limit(self.storage, self.settings.max_storage_gb)
                self.recording_finished.emit(game_row)
                self.status_changed.emit("녹화 저장 완료")
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
            retry_no_space = g.status == "failed" and (g.note or "").startswith(NOTE_NO_SPACE)
            if (g.status not in ("recording", "processing") and not retry_no_space) or not g.folder:
                continue
            # 한 경기의 복구 실패가 나머지 경기 복구를 막지 않도록 경기별로 처리
            try:
                folder = Path(g.folder)
                seg_dir = folder / "segments"
                if ffmpeg is None or not seg_dir.exists():
                    self.storage.update_game(g.id, status="failed", note="녹화가 중단되었습니다")
                    continue
                log.info("중단된 녹화 복구: %s", folder)
                collected, offset, meta = ev.load_events(folder / "events.json")
                self._finalize(g.id, ffmpeg, seg_dir, folder, collected, offset, meta.get("me"))
            except Exception as e:
                log.exception("중단된 녹화 복구 실패: %s", g.folder)
                self.storage.update_game(g.id, status="failed", note=f"복구 실패: {e}")


def folder_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def videos_over_keep(storage: Storage, keep: int) -> list[GameRow]:
    """'최근 N경기 영상만 보관' 설정에 따라 지워질 (오래된) 영상 목록."""
    if keep <= 0:
        return []
    videos = [g for g in storage.list_games() if g.status == "ready" and g.has_video]
    videos.sort(key=lambda g: g.started_at or "", reverse=True)
    return videos[keep:]


def enforce_keep_recent(storage: Storage, keep: int) -> list[int]:
    """최근 keep 경기의 영상만 남기고 이전 영상은 삭제 (경기 기록/통계는 유지). 지운 game id 목록 반환."""
    removed = []
    for g in videos_over_keep(storage, keep):
        try:
            Path(g.video_path).unlink()
        except FileNotFoundError:
            pass
        except OSError:  # 재생 중이라 잠겨 있는 등 -> 다음에 다시 시도
            log.warning("영상 자동 삭제 실패: %s", g.video_path, exc_info=True)
            continue
        storage.update_game(g.id, video_path=None, status="none", note=f"최근 {keep}경기만 보관 설정으로 영상 자동 삭제")
        removed.append(g.id)
    if removed:
        log.info("최근 %d경기만 보관: 영상 %d개 삭제", keep, len(removed))
    return removed


def enforce_storage_limit(storage: Storage, max_gb: float, reserve_bytes: int = 0) -> tuple[list[int], bool]:
    """녹화 폴더들이 차지하는 전체 용량(조각, 임시 파일, 이벤트 포함)이 제한을 넘으면
    오래된 완성 영상부터 삭제한다 (경기 데이터는 유지). reserve_bytes 는 곧 녹화할 분량.

    반환: (영상을 지운 game id 목록, 지울 수 있는 영상을 다 지워도 제한을 넘는지)
    """
    if max_gb <= 0:
        return [], False
    games = storage.list_games()
    folders = {}
    for g in games:
        if g.folder and Path(g.folder).is_dir():
            folders.setdefault(str(Path(g.folder)), g)
    total = sum(folder_size(Path(f)) for f in folders) + reserve_bytes
    limit = max_gb * GB
    removed = []
    # 녹화/정리 중이거나 실패한(조각만 남은) 녹화는 자동으로 지우지 않는다
    candidates = [g for g in games if g.status == "ready" and g.has_video]
    for g in sorted(candidates, key=lambda g: g.started_at or ""):
        if total <= limit:
            break
        try:
            size = Path(g.video_path).stat().st_size
            Path(g.video_path).unlink()
        except OSError:  # 재생 중이라 잠겨 있는 등
            continue
        total -= size
        storage.update_game(g.id, video_path=None, status="none", note="용량 제한으로 영상 자동 삭제")
        removed.append(g.id)
    if removed:
        log.info("용량 제한으로 영상 %d개 삭제", len(removed))
    return removed, total > limit
