"""경기/녹화 메타데이터를 SQLite에 저장. 원본 API JSON은 파일로 따로 둔다."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      TEXT UNIQUE,
    game_id       INTEGER,
    platform      TEXT,
    queue_id      INTEGER,
    champion      TEXT,
    riot_id       TEXT,
    puuid         TEXT,
    started_at    TEXT,
    duration_sec  REAL,
    folder        TEXT,
    video_path    TEXT,
    status        TEXT DEFAULT 'none',     -- recording | processing | ready | failed | none(녹화 없음)
    api_status    TEXT DEFAULT 'pending',  -- pending | done | failed | no_key
    win           INTEGER,
    kills         INTEGER,
    deaths        INTEGER,
    assists       INTEGER,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_games_started ON games(started_at);
"""


@dataclass
class GameRow:
    id: int
    match_id: str | None
    game_id: int | None
    platform: str | None
    queue_id: int | None
    champion: str | None
    riot_id: str | None
    puuid: str | None
    started_at: str | None
    duration_sec: float | None
    folder: str | None
    video_path: str | None
    status: str
    api_status: str
    win: int | None
    kills: int | None
    deaths: int | None
    assists: int | None
    note: str | None

    @property
    def has_video(self) -> bool:
        return bool(self.video_path) and Path(self.video_path).exists()

    @property
    def kda_text(self) -> str:
        if self.kills is None:
            return "-"
        return f"{self.kills}/{self.deaths}/{self.assists}"

    @property
    def started_dt(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.started_at) if self.started_at else None
        except ValueError:
            return None


class Storage:
    def __init__(self, db_path: Path, data_dir: Path):
        self.db_path = db_path
        self.data_dir = data_dir  # 원본 JSON 저장 위치
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ games
    def _execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def create_game(self, **fields) -> int:
        cols = ", ".join(fields)
        marks = ", ".join(f":{k}" for k in fields)
        return self._execute(f"INSERT INTO games ({cols}) VALUES ({marks})", fields).lastrowid

    def update_game(self, game_id: int, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self._execute(f"UPDATE games SET {sets} WHERE id = :_id", {**fields, "_id": game_id})

    def delete_game(self, game_id: int) -> None:
        self._execute("DELETE FROM games WHERE id = ?", (game_id,))

    def get_game(self, game_id: int) -> GameRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return GameRow(**dict(row)) if row else None

    def find_by_match_id(self, match_id: str) -> GameRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM games WHERE match_id = ?", (match_id,)).fetchone()
        return GameRow(**dict(row)) if row else None

    def list_games(self, limit: int | None = None) -> list[GameRow]:
        sql = "SELECT * FROM games ORDER BY started_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [GameRow(**dict(r)) for r in rows]

    def pending_api_games(self) -> list[GameRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM games WHERE api_status IN ('pending','failed','no_key') AND match_id IS NOT NULL"
            ).fetchall()
        return [GameRow(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ raw json
    def match_json_path(self, match_id: str) -> Path:
        return self.data_dir / "matches" / f"{match_id}.json"

    def timeline_json_path(self, match_id: str) -> Path:
        return self.data_dir / "timelines" / f"{match_id}_timeline.json"

    def save_raw(self, match_id: str, match: dict | None, timeline: dict | None) -> None:
        if match is not None:
            p = self.match_json_path(match_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(match, ensure_ascii=False), encoding="utf-8")
        if timeline is not None:
            p = self.timeline_json_path(match_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")

    def load_match(self, match_id: str) -> dict | None:
        p = self.match_json_path(match_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def load_timeline(self, match_id: str) -> dict | None:
        p = self.timeline_json_path(match_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
