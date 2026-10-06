"""롤 클라이언트가 PC 안에서 제공하는 로컬 API.

  * LCU (League Client Update) API : 클라이언트 상태(게임 시작/종료), gameId, 큐 종류, 내 PUUID
  * Live Client Data API (127.0.0.1:2999) : 게임 중 실시간 이벤트와 게임 시간

두 API 모두 Riot의 자체 서명 인증서를 쓰는 루프백(127.0.0.1) 전용 HTTPS 이므로
인증서 검증을 끄고 호출한다. 외부로 나가는 요청에는 사용하지 않는다.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests
import urllib3

log = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GAME_PROCESS = "League of Legends.exe"
CLIENT_PROCESSES = ("LeagueClientUx.exe", "LeagueClient.exe")
DEFAULT_LOCKFILES = [
    Path(r"C:\Riot Games\League of Legends\lockfile"),
    Path(r"D:\Riot Games\League of Legends\lockfile"),
]


def find_process(name: str):
    """이름으로 프로세스 찾기 (psutil.Process 또는 None)."""
    import psutil

    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() == name.lower():
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


# --------------------------------------------------------------------------- LCU

@dataclass
class LcuCredentials:
    port: int
    password: str

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    @property
    def auth_header(self) -> str:
        token = base64.b64encode(f"riot:{self.password}".encode()).decode()
        return f"Basic {token}"


def parse_lockfile(text: str) -> LcuCredentials | None:
    # 형식: LeagueClient:<pid>:<port>:<password>:https
    parts = text.strip().split(":")
    if len(parts) < 5:
        return None
    try:
        return LcuCredentials(int(parts[2]), parts[3])
    except ValueError:
        return None


def parse_cmdline(args: list[str]) -> LcuCredentials | None:
    joined = " ".join(args)
    port = re.search(r"--app-port=(\d+)", joined)
    token = re.search(r"--remoting-auth-token=([\w-]+)", joined)
    if port and token:
        return LcuCredentials(int(port.group(1)), token.group(1))
    return None


def find_lcu_credentials() -> LcuCredentials | None:
    for name in CLIENT_PROCESSES:
        proc = find_process(name)
        if proc is None:
            continue
        try:
            creds = parse_cmdline(proc.cmdline())
            if creds:
                return creds
            # 실행 파일 위치의 lockfile
            lock = Path(proc.exe()).parent / "lockfile"
            if lock.exists():
                return parse_lockfile(lock.read_text())
        except Exception:
            log.debug("LCU 프로세스 정보 읽기 실패", exc_info=True)
    for lock in DEFAULT_LOCKFILES:
        if lock.exists():
            try:
                return parse_lockfile(lock.read_text())
            except OSError:
                pass
    return None


class LcuClient:
    def __init__(self, creds: LcuCredentials, timeout: float = 2.0):
        self.creds = creds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False  # 루프백 전용 자체 서명 인증서
        self.session.headers["Authorization"] = creds.auth_header

    @classmethod
    def connect(cls) -> "LcuClient | None":
        creds = find_lcu_credentials()
        return cls(creds) if creds else None

    def get(self, path: str):
        try:
            r = self.session.get(self.creds.base_url + path, timeout=self.timeout)
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError):
            pass
        return None

    def gameflow_phase(self) -> str | None:
        return self.get("/lol-gameflow/v1/gameflow-phase")

    def session_info(self) -> dict | None:
        return self.get("/lol-gameflow/v1/session")

    def current_summoner(self) -> dict | None:
        return self.get("/lol-summoner/v1/current-summoner")

    def region(self) -> str | None:
        info = self.get("/riotclient/region-locale")
        return info.get("region") if isinstance(info, dict) else None


@dataclass
class GameSessionInfo:
    game_id: int | None = None
    queue_id: int | None = None
    queue_name: str = ""
    map_id: int | None = None
    puuid: str = ""
    riot_id: str = ""
    region: str | None = None


def read_session(lcu: LcuClient | None) -> GameSessionInfo:
    info = GameSessionInfo()
    if lcu is None:
        return info
    sess = lcu.session_info() or {}
    game = sess.get("gameData") or {}
    info.game_id = game.get("gameId") or None
    queue = game.get("queue") or {}
    info.queue_id = queue.get("id")
    info.queue_name = queue.get("description") or queue.get("type") or ""
    info.map_id = (sess.get("map") or {}).get("id")
    me = lcu.current_summoner() or {}
    info.puuid = me.get("puuid", "")
    if me.get("gameName"):
        info.riot_id = f"{me['gameName']}#{me.get('tagLine', '')}"
    info.region = lcu.region()
    return info


# --------------------------------------------------------------------------- Live Client

class LiveClient:
    BASE = "https://127.0.0.1:2999/liveclientdata"

    def __init__(self, timeout: float = 1.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False  # 루프백 전용 자체 서명 인증서

    def _get(self, path: str):
        try:
            r = self.session.get(f"{self.BASE}/{path}", timeout=self.timeout)
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError):
            pass
        return None

    def game_stats(self) -> dict | None:
        return self._get("gamestats")

    def events(self) -> list[dict]:
        data = self._get("eventdata")
        return (data or {}).get("Events", []) if isinstance(data, dict) else []

    def active_player_name(self) -> str | None:
        return self._get("activeplayername")

    def player_list(self) -> list[dict]:
        data = self._get("playerlist")
        return data if isinstance(data, list) else []

    def is_available(self) -> bool:
        return self.game_stats() is not None


def game_window_mode() -> str | None:
    """game.cfg 에서 창 모드 읽기: 'fullscreen' | 'windowed' | 'borderless' | None."""
    candidates = [Path(r"C:\Riot Games\League of Legends\Config\game.cfg")]
    proc = find_process(GAME_PROCESS)
    if proc is not None:
        try:
            candidates.insert(0, Path(proc.exe()).parent.parent / "Config" / "game.cfg")
        except Exception:
            pass
    for cfg in candidates:
        try:
            if cfg.exists():
                m = re.search(r"^WindowMode=(\d)", cfg.read_text(errors="ignore"), re.M)
                if m:
                    return {"0": "fullscreen", "1": "windowed", "2": "borderless"}.get(m.group(1))
        except OSError:
            continue
    return None


def game_pid() -> int | None:
    p = find_process(GAME_PROCESS)
    return p.pid if p else None


IS_WINDOWS = os.name == "nt"
