"""경기 후 Riot API 로 match / timeline 을 받아 저장하는 백그라운드 작업자."""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import analysis
from . import events as ev
from .config import Settings, get_api_key
from .riot.api import RequestCancelled, RiotApi, RiotApiError
from .storage import Storage

log = logging.getLogger(__name__)

# 경기 직후에는 데이터가 아직 없을 수 있어(404) 간격을 두고 재시도
RETRY_DELAYS = [20, 30, 30, 60, 60, 120, 120, 300]


class MatchFetcher(QObject):
    game_updated = Signal(int)
    status = Signal(str)
    error = Signal(str)
    import_finished = Signal(int)

    def __init__(self, settings: Settings, storage: Storage):
        super().__init__()
        self.settings = settings
        self.storage = storage
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="match-fetcher")
        self._thread.start()
        # 이전에 실패/보류된 경기 재시도
        for g in self.storage.pending_api_games():
            self.enqueue(g.id, immediate=True)

    def stop(self, timeout: float | None = None) -> bool:
        """작업자를 멈추고 실제로 끝날 때까지 기다린다. 끝났으면 True.

        재시도/요청 제한 대기는 즉시 중단되고, 진행 중인 HTTP 요청은 요청 타임아웃 안에 끝난다.
        """
        self._stop.set()
        self._queue.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
        stopped = not (self._thread and self._thread.is_alive())
        if not stopped:
            log.warning("Riot API 작업자가 제한 시간 안에 끝나지 않음")
        return stopped

    def enqueue(self, game_row_id: int, immediate: bool = False) -> None:
        self._queue.put(("game", game_row_id, 0 if immediate else RETRY_DELAYS[0], 0))

    def import_recent(self, count: int, queue_id: int | None = None) -> None:
        self._queue.put(("import", count, queue_id, 0))

    def api(self) -> RiotApi | None:
        key = get_api_key()
        if not key:
            return None
        return RiotApi(key, self.settings.region if self.settings.region != "auto" else "KR", cancel=self._stop)

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        delayed: list[tuple[float, tuple]] = []
        while not self._stop.is_set():
            now = time.monotonic()
            due = [d for d in delayed if d[0] <= now]
            delayed = [d for d in delayed if d[0] > now]
            for _, job in due:
                self._queue.put(job)
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                break
            kind = job[0]
            try:
                if kind == "game":
                    _, gid, delay, attempt = job
                    if delay:
                        delayed.append((time.monotonic() + delay, ("game", gid, 0, attempt)))
                        continue
                    retry = self._fetch_game(gid, attempt)
                    if retry is not None:
                        delayed.append((time.monotonic() + retry, ("game", gid, 0, attempt + 1)))
                elif kind == "import":
                    _, count, queue_id, _ = job
                    self._import(count, queue_id)
            except RequestCancelled:
                break  # 앱 종료 중
            except Exception as e:
                log.exception("Riot API 작업 실패")
                self.error.emit(f"Riot API 오류: {e}")

    def _fetch_game(self, gid: int, attempt: int) -> float | None:
        """성공/영구 실패면 None, 재시도가 필요하면 대기 초를 반환."""
        game = self.storage.get_game(gid)
        if game is None or not game.match_id:
            return None
        api = self.api()
        if api is None:
            self.storage.update_game(gid, api_status="no_key")
            self.status.emit("API 키가 없어 경기 데이터를 받지 못했습니다 (설정에서 입력)")
            return None
        if game.platform:
            api.platform = game.platform
        try:
            match = api.match(game.match_id)
            timeline = api.timeline(game.match_id)
        except RiotApiError as e:
            if e.status in (404, 0, 429, 500, 502, 503, 504) and attempt < len(RETRY_DELAYS) - 1:
                self.status.emit(f"경기 데이터 대기 중... ({game.match_id})")
                return RETRY_DELAYS[attempt + 1]
            if e.status in (401, 403):
                self.error.emit("API 키가 유효하지 않습니다. 설정에서 키를 확인하세요.")
            self.storage.update_game(gid, api_status="failed", note=str(e))
            return None
        self.storage.save_raw(game.match_id, match, timeline)
        self._apply_match(gid, match, timeline)
        self.status.emit(f"경기 데이터 저장 완료 ({game.match_id})")
        return None

    def _apply_match(self, gid: int, match: dict, timeline: dict | None) -> None:
        game = self.storage.get_game(gid)
        puuid = (game.puuid if game else "") or self.settings.puuid
        riot_id = (game.riot_id if game else "") or self.settings.riot_id
        summary = analysis.summarize(match, puuid, riot_id)
        fields = {"api_status": "done"}
        info = match.get("info") or {}
        fields["queue_id"] = info.get("queueId")
        fields["duration_sec"] = (game.duration_sec if game and game.duration_sec else
                                  analysis.game_duration_sec(match))
        if summary:
            fields.update(champion=summary.champion, win=int(summary.win), kills=summary.kills,
                          deaths=summary.deaths, assists=summary.assists)
        self.storage.update_game(gid, **fields)

        # 녹화가 있으면 timeline 이벤트(와드/아이템/레벨업)를 재생바에 추가
        if game and game.folder and timeline is not None:
            me = analysis.find_me(match, puuid, riot_id)
            events_file = Path(game.folder) / "events.json"
            if me and events_file.exists():
                live_events, offset, meta = ev.load_events(events_file)
                champs, teams = analysis.participant_maps(match)
                tl_events = ev.from_timeline(timeline, me["participantId"], champs, teams.get(me["participantId"]))
                merged = ev.merge_timeline_events(live_events, tl_events)
                duration = game.duration_sec
                kept = []
                for e in merged:
                    vt = e.game_time + offset
                    if vt < 0 or (duration and vt > duration):
                        continue
                    e.video_time = vt
                    kept.append(e)
                meta["timeline_merged"] = True
                ev.save_events(events_file, kept, offset, meta)
        self.game_updated.emit(gid)

    def _import(self, count: int, queue_id: int | None) -> None:
        api = self.api()
        if api is None:
            self.error.emit("API 키를 먼저 설정하세요")
            return
        puuid = self.settings.puuid
        if not puuid:
            if not self.settings.riot_id:
                self.error.emit("Riot ID(이름#태그)를 설정하거나 롤 클라이언트를 켠 상태에서 한 판 진행해 주세요")
                return
            puuid = api.puuid_from_riot_id(self.settings.riot_id)
            self.settings.puuid = puuid
            from .config import save_settings

            save_settings(self.settings)
        ids = api.match_ids(puuid, count=count, queue=queue_id)
        added = 0
        for i, mid in enumerate(ids, 1):
            if self._stop.is_set():
                break
            self.status.emit(f"과거 경기 가져오는 중 {i}/{len(ids)}")
            existing = self.storage.find_by_match_id(mid)
            if existing and existing.api_status == "done":
                continue
            match = api.match(mid)
            timeline = api.timeline(mid)
            self.storage.save_raw(mid, match, timeline)
            if existing:
                gid = existing.id
            else:
                info = match.get("info") or {}
                start_ms = info.get("gameStartTimestamp") or info.get("gameCreation") or 0
                from datetime import datetime

                gid = self.storage.create_game(
                    match_id=mid, game_id=info.get("gameId"), platform=mid.split("_")[0],
                    queue_id=info.get("queueId"), puuid=puuid, riot_id=self.settings.riot_id,
                    started_at=datetime.fromtimestamp(start_ms / 1000).isoformat(timespec="seconds"),
                    status="none", api_status="pending",
                )
            self._apply_match(gid, match, timeline)
            added += 1
        self.status.emit(f"과거 경기 {added}개 가져오기 완료")
        self.import_finished.emit(added)
