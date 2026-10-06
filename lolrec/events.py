"""재생바에 표시할 이벤트 모델과 변환 로직.

이벤트 출처는 두 가지다.
  * Live Client Data API (게임 중 실시간) - 킬/데스/오브젝트 등
  * Match-v5 timeline (경기 후 Riot API) - 와드, 아이템, 레벨업 등 추가 정보
둘 다 "게임 시간"을 쓰므로 offset(영상 시간 - 게임 시간)을 더해 영상 위치로 바꾼다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# 이벤트 종류 -> (표시 이름, 색상)
EVENT_TYPES: dict[str, tuple[str, str]] = {
    "kill": ("내 킬", "#3fb950"),
    "death": ("내 데스", "#f85149"),
    "assist": ("내 어시스트", "#58a6ff"),
    "multikill": ("멀티킬", "#ffd33d"),
    "first_blood": ("퍼스트 블러드", "#ff7b72"),
    "ace": ("에이스", "#d2a8ff"),
    "ally_kill": ("아군 킬", "#4f8a5b"),
    "enemy_kill": ("적군 킬", "#a5524f"),
    "dragon": ("드래곤", "#f0883e"),
    "baron": ("바론", "#a371f7"),
    "herald": ("전령/유충", "#bc8cff"),
    "atakhan": ("아타칸", "#db61a2"),
    "turret": ("포탑", "#8b949e"),
    "inhibitor": ("억제기", "#6e7681"),
    "ward": ("와드", "#e3b341"),
    "item": ("아이템 구매", "#79c0ff"),
    "level": ("레벨업", "#56d4dd"),
    "game_end": ("게임 종료", "#ffffff"),
}

# 기본으로 재생바에 보이는 종류 (와드/아이템은 너무 많아서 기본 숨김)
DEFAULT_VISIBLE = {
    "kill", "death", "assist", "multikill", "first_blood", "ace",
    "dragon", "baron", "herald", "atakhan", "turret", "inhibitor", "game_end",
}


@dataclass
class GameEvent:
    type: str
    game_time: float  # 초
    label: str
    source: str = "live"  # live | timeline
    data: dict = field(default_factory=dict)
    video_time: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameEvent":
        return cls(**{k: d.get(k) for k in ("type", "game_time", "label", "source", "data", "video_time")})


# --------------------------------------------------------------------------- 내 정보 매칭

class PlayerIdentity:
    """Live Client 이벤트의 이름(KillerName 등)이 '나'인지, 아군인지 판단."""

    def __init__(self, me: str = "", players: Iterable[dict] = ()):
        self.my_names: set[str] = set()
        self.team_of: dict[str, str] = {}
        self.champion_of: dict[str, str] = {}
        self.my_team: str | None = None
        if me:
            self._add_names(self.my_names, me)
        for p in players:
            names = self._player_names(p)
            team = p.get("team")
            for n in names:
                self.team_of[n] = team
                self.champion_of[n] = p.get("championName", "")
        for n in self.my_names:
            if n in self.team_of:
                self.my_team = self.team_of[n]
                break

    @staticmethod
    def _norm(name: str) -> str:
        return name.strip().casefold()

    def _add_names(self, target: set[str], full: str) -> None:
        if not full:
            return
        target.add(self._norm(full))
        if "#" in full:
            target.add(self._norm(full.split("#", 1)[0]))

    def _player_names(self, p: dict) -> set[str]:
        out: set[str] = set()
        for key in ("riotId", "riotIdGameName", "summonerName"):
            self._add_names(out, p.get(key) or "")
        return out

    def is_me(self, name: str | None) -> bool:
        return bool(name) and self._norm(name) in self.my_names

    def is_ally(self, name: str | None) -> bool | None:
        if not name:
            return None
        team = self.team_of.get(self._norm(name))
        if team is None or self.my_team is None:
            return None
        return team == self.my_team

    def champion(self, name: str | None) -> str:
        if not name:
            return ""
        return self.champion_of.get(self._norm(name), "") or name


# --------------------------------------------------------------------------- Live Client

DRAGON_KO = {
    "Fire": "화염", "Earth": "대지", "Water": "바다", "Air": "바람",
    "Hextech": "마법공학", "Chemtech": "화학공학", "Elder": "장로",
}


def from_live_event(ev: dict, who: PlayerIdentity) -> GameEvent | None:
    """Live Client eventdata 항목 하나를 GameEvent로 변환. 표시할 필요 없으면 None."""
    name = ev.get("EventName", "")
    t = float(ev.get("EventTime", 0.0))
    killer = ev.get("KillerName")
    assisters = ev.get("Assisters") or []
    data = {k: v for k, v in ev.items() if k not in ("EventName", "EventTime")}

    if name == "ChampionKill":
        victim = ev.get("VictimName")
        kc, vc = who.champion(killer), who.champion(victim)
        if who.is_me(victim):
            return GameEvent("death", t, f"사망 ({kc}에게)", data=data)
        if who.is_me(killer):
            return GameEvent("kill", t, f"킬: {vc}", data=data)
        if any(who.is_me(a) for a in assisters):
            return GameEvent("assist", t, f"어시스트: {kc} → {vc}", data=data)
        ally = who.is_ally(killer)
        etype = "ally_kill" if ally else "enemy_kill"
        return GameEvent(etype, t, f"{kc} → {vc}", data=data)
    if name == "Multikill":
        n = int(ev.get("KillStreak", 2))
        label = {2: "더블킬", 3: "트리플킬", 4: "쿼드라킬", 5: "펜타킬"}.get(n, f"{n}연속 킬")
        who_str = "나" if who.is_me(killer) else who.champion(killer)
        return GameEvent("multikill", t, f"{label} ({who_str})", data=data)
    if name == "FirstBlood":
        return GameEvent("first_blood", t, f"퍼스트 블러드 ({who.champion(ev.get('Recipient'))})", data=data)
    if name == "Ace":
        return GameEvent("ace", t, f"에이스 ({ev.get('AcingTeam', '')})", data=data)
    if name == "DragonKill":
        kind = DRAGON_KO.get(ev.get("DragonType", ""), ev.get("DragonType", ""))
        stolen = " (스틸)" if str(ev.get("Stolen", "False")).lower() == "true" else ""
        side = _side(who, killer)
        return GameEvent("dragon", t, f"{side}{kind} 드래곤{stolen}", data=data)
    if name == "BaronKill":
        return GameEvent("baron", t, f"{_side(who, killer)}바론", data=data)
    if name == "HeraldKill":
        return GameEvent("herald", t, f"{_side(who, killer)}전령", data=data)
    if name in ("HordeKill", "VoidGrubKill"):
        return GameEvent("herald", t, f"{_side(who, killer)}공허 유충", data=data)
    if name == "AtakhanKill":
        return GameEvent("atakhan", t, f"{_side(who, killer)}아타칸", data=data)
    if name == "TurretKilled":
        return GameEvent("turret", t, f"포탑 파괴 ({_structure_side(ev.get('TurretKilled', ''), who)})", data=data)
    if name == "InhibKilled":
        return GameEvent("inhibitor", t, f"억제기 파괴 ({_structure_side(ev.get('InhibKilled', ''), who)})", data=data)
    if name == "GameEnd":
        result = {"Win": "승리", "Lose": "패배"}.get(ev.get("Result", ""), ev.get("Result", ""))
        return GameEvent("game_end", t, f"게임 종료 - {result}", data=data)
    return None


def _side(who: PlayerIdentity, killer: str | None) -> str:
    ally = who.is_ally(killer)
    if ally is True:
        return "아군 "
    if ally is False:
        return "적 "
    return ""


def _structure_side(structure: str, who: PlayerIdentity) -> str:
    # Turret_T1_... = ORDER(블루) 구조물, Turret_T2_... = CHAOS(레드)
    owner = "ORDER" if "_T1_" in structure else "CHAOS" if "_T2_" in structure else None
    if owner is None or who.my_team is None:
        return structure
    return "아군 구조물" if owner == who.my_team else "적 구조물"


# --------------------------------------------------------------------------- Match-v5 timeline

MONSTER_TYPES = {
    "DRAGON": ("dragon", "드래곤"),
    "BARON_NASHOR": ("baron", "바론"),
    "RIFTHERALD": ("herald", "전령"),
    "HORDE": ("herald", "공허 유충"),
    "ATAKHAN": ("atakhan", "아타칸"),
}


def from_timeline(timeline: dict, my_participant_id: int, champions: dict[int, str] | None = None,
                  my_team_id: int | None = None) -> list[GameEvent]:
    """timeline에서 Live Client에 없는 정보(와드/아이템/레벨업)와 오브젝트를 추출."""
    champions = champions or {}
    out: list[GameEvent] = []
    frames = (timeline.get("info") or {}).get("frames") or []
    for frame in frames:
        for ev in frame.get("events", []):
            et = ev.get("type")
            t = ev.get("timestamp", 0) / 1000.0
            pid = ev.get("participantId") or ev.get("creatorId") or ev.get("killerId")
            if et == "WARD_PLACED" and ev.get("creatorId") == my_participant_id:
                if ev.get("wardType") in ("UNDEFINED", None):
                    continue
                out.append(GameEvent("ward", t, f"와드 설치 ({ev.get('wardType')})", "timeline", ev))
            elif et == "WARD_KILL" and ev.get("killerId") == my_participant_id:
                out.append(GameEvent("ward", t, f"와드 제거 ({ev.get('wardType')})", "timeline", ev))
            elif et == "ITEM_PURCHASED" and pid == my_participant_id:
                out.append(GameEvent("item", t, f"아이템 구매 #{ev.get('itemId')}", "timeline", ev))
            elif et == "LEVEL_UP" and pid == my_participant_id:
                out.append(GameEvent("level", t, f"레벨 {ev.get('level')}", "timeline", ev))
            elif et == "ELITE_MONSTER_KILL":
                etype, name = MONSTER_TYPES.get(ev.get("monsterType", ""), (None, None))
                if etype is None:
                    continue
                if ev.get("monsterSubType"):
                    name = f"{name} ({ev['monsterSubType'].replace('_DRAGON', '').title()})"
                side = ""
                if my_team_id is not None and ev.get("killerTeamId"):
                    side = "아군 " if ev["killerTeamId"] == my_team_id else "적 "
                out.append(GameEvent(etype, t, f"{side}{name}", "timeline", ev))
            elif et == "CHAMPION_KILL":
                victim, killer = ev.get("victimId"), ev.get("killerId")
                assists = ev.get("assistingParticipantIds") or []
                vc, kc = champions.get(victim, f"#{victim}"), champions.get(killer, f"#{killer}")
                if victim == my_participant_id:
                    out.append(GameEvent("death", t, f"사망 ({kc}에게)", "timeline", ev))
                elif killer == my_participant_id:
                    out.append(GameEvent("kill", t, f"킬: {vc}", "timeline", ev))
                elif my_participant_id in assists:
                    out.append(GameEvent("assist", t, f"어시스트: {kc} → {vc}", "timeline", ev))
            elif et == "BUILDING_KILL":
                etype = "inhibitor" if ev.get("buildingType") == "INHIBITOR_BUILDING" else "turret"
                name = "억제기 파괴" if etype == "inhibitor" else "포탑 파괴"
                side = ""
                if my_team_id is not None and ev.get("teamId"):
                    # teamId = 파괴된 구조물의 소유 팀
                    side = " (아군 구조물)" if ev["teamId"] == my_team_id else " (적 구조물)"
                out.append(GameEvent(etype, t, name + side, "timeline", ev))
    return out


def merge_timeline_events(live: list[GameEvent], timeline: list[GameEvent],
                          tolerance: float = 3.0) -> list[GameEvent]:
    """live 이벤트를 우선 사용하고, timeline에서 새로운 이벤트만 추가.

    킬/오브젝트처럼 양쪽에 모두 있는 종류는 live에 없는 경우에만(녹화 중간에 앱을 켰다든지) 추가한다.
    """
    merged = list(live)
    for ev in timeline:
        dup = any(
            l.type == ev.type and abs(l.game_time - ev.game_time) <= tolerance for l in live
        )
        if not dup:
            merged.append(ev)
    merged.sort(key=lambda e: e.game_time)
    return merged


def apply_offset(events: list[GameEvent], offset: float, duration: float | None = None) -> None:
    for e in events:
        vt = e.game_time + offset
        if duration is not None:
            vt = min(max(vt, 0.0), duration)
        e.video_time = max(vt, 0.0)


# --------------------------------------------------------------------------- 저장

def save_events(path: Path, events: list[GameEvent], offset: float, meta: dict | None = None) -> None:
    """임시 파일에 쓴 뒤 교체해서, 쓰는 도중 꺼져도 파일이 반쯤 잘리지 않게 한다."""
    payload = {"offset": offset, "meta": meta or {}, "events": [e.to_dict() for e in events]}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: 다른 쪽에서 잠깐 읽고 있는 중
            if attempt == 4:
                raise
            time.sleep(0.05)


def load_events(path: Path) -> tuple[list[GameEvent], float, dict]:
    """이벤트 파일을 읽는다. 손상된 파일이면 경고만 남기고 빈 결과를 돌려준다."""
    if not path.exists():
        return [], 0.0, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        events = [GameEvent.from_dict(d) for d in payload.get("events", [])]
        return events, float(payload.get("offset", 0.0)), payload.get("meta", {})
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        log.warning("이벤트 파일이 손상되어 무시합니다: %s", path, exc_info=True)
        return [], 0.0, {}
