"""Raw 데이터 내보내기.

선택한 경기들을 폴더 하나로 내보낸다:
  raw/matches/*.json, raw/timelines/*.json   Riot API 원본 그대로
  participants.csv   경기당 10명 전원의 스탯 (match 의 participants 평탄화)
  teams.csv          팀 단위 (오브젝트, 밴)
  frames.csv         분 단위 참가자 프레임 (골드, CS, 레벨, 위치, 데미지)
  events.csv         timeline 이벤트 전체
  recording_events.csv  녹화 중 수집한 이벤트 + 영상 시간
pandas: pd.read_csv("participants.csv") 등으로 바로 분석 가능 (UTF-8 BOM, 엑셀에서도 한글 깨짐 없음)
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Iterable

from .events import load_events
from .storage import GameRow, Storage


def flatten(d: dict, prefix: str = "", out: dict | None = None) -> dict:
    """중첩 dict 를 'a.b.c' 키로 평탄화. 리스트는 JSON 문자열로."""
    out = {} if out is None else out
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            flatten(v, key + ".", out)
        elif isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False)
        else:
            out[key] = v
    return out


def write_csv(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    columns: list[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                columns.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def participant_rows(match: dict, my_puuid: str = "") -> list[dict]:
    meta, info = match.get("metadata") or {}, match.get("info") or {}
    rows = []
    for p in info.get("participants") or []:
        base = {
            "matchId": meta.get("matchId"),
            "gameStartTimestamp": info.get("gameStartTimestamp"),
            "gameDuration": info.get("gameDuration"),
            "gameVersion": info.get("gameVersion"),
            "queueId": info.get("queueId"),
            "isMe": bool(my_puuid) and p.get("puuid") == my_puuid,
        }
        rows.append(flatten(p, out=base))
    return rows


def team_rows(match: dict) -> list[dict]:
    meta, info = match.get("metadata") or {}, match.get("info") or {}
    return [flatten(t, out={"matchId": meta.get("matchId")}) for t in info.get("teams") or []]


def frame_rows(match_id: str, timeline: dict) -> list[dict]:
    rows = []
    for i, frame in enumerate((timeline.get("info") or {}).get("frames") or []):
        for pid, pf in (frame.get("participantFrames") or {}).items():
            rows.append(flatten(pf, out={"matchId": match_id, "minute": i, "timestamp": frame.get("timestamp"),
                                         "participantId": int(pid)}))
    return rows


def event_rows(match_id: str, timeline: dict) -> list[dict]:
    rows = []
    for i, frame in enumerate((timeline.get("info") or {}).get("frames") or []):
        for e in frame.get("events") or []:
            rows.append(flatten(e, out={"matchId": match_id, "frame": i}))
    return rows


def export_games(storage: Storage, games: Iterable[GameRow], out_dir: Path, my_puuid: str = "") -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw" / "matches").mkdir(parents=True, exist_ok=True)
    (out_dir / "raw" / "timelines").mkdir(parents=True, exist_ok=True)
    participants, teams, frames, events, rec_events = [], [], [], [], []
    for g in games:
        if g.match_id:
            match = storage.load_match(g.match_id)
            timeline = storage.load_timeline(g.match_id)
            if match:
                shutil.copy2(storage.match_json_path(g.match_id), out_dir / "raw" / "matches")
                participants += participant_rows(match, g.puuid or my_puuid)
                teams += team_rows(match)
            if timeline:
                shutil.copy2(storage.timeline_json_path(g.match_id), out_dir / "raw" / "timelines")
                frames += frame_rows(g.match_id, timeline)
                events += event_rows(g.match_id, timeline)
        if g.folder:
            evs, offset, _ = load_events(Path(g.folder) / "events.json")
            for e in evs:
                rec_events.append({
                    "matchId": g.match_id, "recording": Path(g.folder).name, "type": e.type,
                    "label": e.label, "gameTime": round(e.game_time, 2),
                    "videoTime": round(e.video_time, 2) if e.video_time is not None else None,
                    "source": e.source, "data": json.dumps(e.data, ensure_ascii=False),
                })
    return {
        "participants.csv": write_csv(out_dir / "participants.csv", participants),
        "teams.csv": write_csv(out_dir / "teams.csv", teams),
        "frames.csv": write_csv(out_dir / "frames.csv", frames),
        "events.csv": write_csv(out_dir / "events.csv", events),
        "recording_events.csv": write_csv(out_dir / "recording_events.csv", rec_events),
    }
