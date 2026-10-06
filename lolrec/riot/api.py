"""Riot Games Web API 클라이언트 (personal API key 용).

  * Account-v1  : Riot ID -> PUUID
  * Match-v5    : 경기 상세 / 타임라인 / 경기 목록
요청 제한(personal key 기본값 20회/1초, 100회/2분)을 지키도록 슬라이딩 윈도우로 조절하고,
429 응답이 오면 Retry-After 만큼 기다렸다가 재시도한다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

# 플랫폼 -> 지역 라우팅 (match-v5)
PLATFORM_ROUTING = {
    "KR": "asia", "JP1": "asia",
    "NA1": "americas", "BR1": "americas", "LA1": "americas", "LA2": "americas",
    "EUW1": "europe", "EUN1": "europe", "TR1": "europe", "RU": "europe", "ME1": "europe",
    "OC1": "sea", "SG2": "sea", "TW2": "sea", "VN2": "sea", "PH2": "sea", "TH2": "sea",
}
# account-v1 은 sea 라우팅을 지원하지 않는다
ACCOUNT_ROUTING = {"asia": "asia", "americas": "americas", "europe": "europe", "sea": "asia"}

# LCU /riotclient/region-locale 의 region 값 -> 플랫폼 ID
LCU_REGION_TO_PLATFORM = {
    "KR": "KR", "JP": "JP1", "NA": "NA1", "BR": "BR1", "LA1": "LA1", "LA2": "LA2", "LAN": "LA1",
    "LAS": "LA2", "EUW": "EUW1", "EUNE": "EUN1", "TR": "TR1", "RU": "RU", "ME1": "ME1",
    "OC1": "OC1", "OCE": "OC1", "SG2": "SG2", "SG": "SG2", "TW2": "TW2", "TW": "TW2",
    "VN2": "VN2", "VN": "VN2", "PH2": "PH2", "PH": "PH2", "TH2": "TH2", "TH": "TH2",
}

PLATFORMS = list(PLATFORM_ROUTING)


def normalize_platform(value: str | None) -> str:
    if not value:
        return "KR"
    v = value.upper()
    if v in PLATFORM_ROUTING:
        return v
    return LCU_REGION_TO_PLATFORM.get(v, "KR")


def routing_for(platform: str) -> str:
    return PLATFORM_ROUTING.get(normalize_platform(platform), "asia")


class RiotApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status


class RateLimiter:
    """여러 개의 (횟수, 초) 제한을 동시에 지키는 슬라이딩 윈도우 리미터."""

    def __init__(self, limits: list[tuple[int, float]], clock=time.monotonic, sleep=time.sleep):
        self.limits = limits
        self.clock = clock
        self.sleep = sleep
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()
        self.blocked_until = 0.0

    def wait_time(self) -> float:
        now = self.clock()
        longest = max(w for _, w in self.limits)
        while self.calls and now - self.calls[0] >= longest:
            self.calls.popleft()
        wait = max(0.0, self.blocked_until - now)
        for count, window in self.limits:
            in_window = [c for c in self.calls if now - c < window]
            if len(in_window) >= count:
                wait = max(wait, window - (now - in_window[-count]))
        return wait

    def acquire(self) -> None:
        with self.lock:
            while True:
                w = self.wait_time()
                if w <= 0:
                    break
                self.sleep(min(w, 5.0))
            self.calls.append(self.clock())

    def block_for(self, seconds: float) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, self.clock() + seconds)


class RiotApi:
    def __init__(self, api_key: str, platform: str = "KR",
                 limits: list[tuple[int, float]] | None = None, timeout: float = 10.0):
        self.api_key = api_key.strip()
        self.platform = normalize_platform(platform)
        self.limiter = RateLimiter(limits or [(20, 1.0), (100, 120.0)])
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-Riot-Token"] = self.api_key

    # ------------------------------------------------------------------ base
    def _get(self, url: str, params: dict | None = None, retries: int = 3):
        for attempt in range(retries + 1):
            self.limiter.acquire()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt >= retries:
                    raise RiotApiError(0, f"네트워크 오류: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "5"))
                log.warning("Riot API 요청 제한 - %.0f초 대기", wait)
                self.limiter.block_for(wait)
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise RiotApiError(r.status_code, _error_message(r))
        raise RiotApiError(429, "요청 제한으로 재시도 횟수를 초과했습니다")

    @property
    def regional(self) -> str:
        return f"https://{routing_for(self.platform)}.api.riotgames.com"

    @property
    def account_host(self) -> str:
        return f"https://{ACCOUNT_ROUTING[routing_for(self.platform)]}.api.riotgames.com"

    # ------------------------------------------------------------------ endpoints
    def account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        url = f"{self.account_host}/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"
        return self._get(url)

    def puuid_from_riot_id(self, riot_id: str) -> str:
        if "#" not in riot_id:
            raise ValueError("Riot ID는 '이름#태그' 형식이어야 합니다")
        name, tag = riot_id.rsplit("#", 1)
        return self.account_by_riot_id(name.strip(), tag.strip())["puuid"]

    def match(self, match_id: str) -> dict:
        return self._get(f"{self.regional}/lol/match/v5/matches/{match_id}")

    def timeline(self, match_id: str) -> dict:
        return self._get(f"{self.regional}/lol/match/v5/matches/{match_id}/timeline")

    def match_ids(self, puuid: str, count: int = 20, start: int = 0, queue: int | None = None) -> list[str]:
        params: dict = {"start": start, "count": min(count, 100)}
        if queue is not None:
            params["queue"] = queue
        return self._get(f"{self.regional}/lol/match/v5/matches/by-puuid/{puuid}/ids", params)

    def check_key(self) -> tuple[bool, str]:
        """키 유효성 확인 (가벼운 요청 1회)."""
        try:
            self._get(f"{self.account_host}/riot/account/v1/accounts/by-riot-id/Hide%20on%20bush/KR1", retries=0)
            return True, "API 키가 정상입니다"
        except RiotApiError as e:
            if e.status == 404:  # 키는 유효, 계정만 없음
                return True, "API 키가 정상입니다"
            if e.status in (401, 403):
                return False, "API 키가 유효하지 않거나 만료되었습니다"
            return False, str(e)


def match_id_for(platform: str, game_id: int) -> str:
    return f"{normalize_platform(platform)}_{game_id}"


def _error_message(r: requests.Response) -> str:
    try:
        return r.json().get("status", {}).get("message", r.text[:200])
    except ValueError:
        return r.text[:200]
