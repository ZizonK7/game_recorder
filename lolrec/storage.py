"""경기/녹화 메타데이터를 SQLite에 저장. 원본 API JSON은 파일로 따로 둔다."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

RAW_SUBDIRS = ("matches", "timelines")

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      TEXT,
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
    note          TEXT,
    session       INTEGER NOT NULL DEFAULT 1  -- 같은 경기를 앱 재시작 등으로 나눠 녹화한 경우 2, 3 ...
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_games_started ON games(started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_games_match_session ON games(match_id, session);
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
    session: int = 1

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
        self._data_lock = threading.RLock()  # 원본 JSON 이동 중 읽기/쓰기 방지
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.executescript(INDEXES)
        self._conn.commit()

    def _migrate(self) -> None:
        """이전 버전 DB(match_id UNIQUE, session 없음)를 현재 구조로 변환."""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(games)")]
        if "session" in cols:
            return
        old_cols = ", ".join(cols)
        self._conn.executescript(f"""
            BEGIN;
            ALTER TABLE games RENAME TO games_old;
            {SCHEMA}
            INSERT INTO games ({old_cols}) SELECT {old_cols} FROM games_old;
            DROP TABLE games_old;
            COMMIT;
        """)

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
            row = self._conn.execute("SELECT * FROM games WHERE match_id = ? ORDER BY session LIMIT 1",
                                     (match_id,)).fetchone()
        return GameRow(**dict(row)) if row else None

    def next_session(self, match_id: str | None) -> int:
        """같은 경기의 다음 녹화 번호 (재시작 후 같은 경기에 다시 들어온 경우 2, 3 ...)."""
        if not match_id:
            return 1
        with self._lock:
            row = self._conn.execute("SELECT MAX(session) FROM games WHERE match_id = ?", (match_id,)).fetchone()
        return (row[0] or 0) + 1

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
        with self._data_lock:
            for p, data in ((self.match_json_path(match_id), match), (self.timeline_json_path(match_id), timeline)):
                if data is None:
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(p)

    def load_match(self, match_id: str) -> dict | None:
        with self._data_lock:
            p = self.match_json_path(match_id)
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def load_timeline(self, match_id: str) -> dict | None:
        with self._data_lock:
            p = self.timeline_json_path(match_id)
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def delete_raw(self, match_id: str) -> None:
        with self._data_lock:
            self.match_json_path(match_id).unlink(missing_ok=True)
            self.timeline_json_path(match_id).unlink(missing_ok=True)

    def relocate_data(self, new_dir: Path) -> int:
        """원본 JSON 저장 위치를 바꾸고 기존 파일을 새 위치로 옮긴다. 옮긴 파일 수를 반환."""
        with self._data_lock:
            old = self.data_dir
            self.data_dir = new_dir
            new_dir.mkdir(parents=True, exist_ok=True)
            return self.import_data_from(old)

    def import_data_from(self, old_dir: Path) -> int:
        """다른 위치에 남아 있는 원본 JSON 을 현재 위치로 옮긴다 (같은 파일이 이미 있으면 그대로 둠)."""
        moved = 0
        with self._data_lock:
            try:
                if not old_dir.exists() or old_dir.resolve() == self.data_dir.resolve():
                    return 0
            except OSError:
                return 0
            for sub in RAW_SUBDIRS:
                src_dir, dst_dir = old_dir / sub, self.data_dir / sub
                if not src_dir.is_dir():
                    continue
                dst_dir.mkdir(parents=True, exist_ok=True)
                for f in src_dir.glob("*.json"):
                    dst = dst_dir / f.name
                    if dst.exists():
                        continue
                    try:
                        shutil.move(str(f), str(dst))
                        moved += 1
                    except OSError:
                        log.warning("원본 데이터 이동 실패: %s", f, exc_info=True)
                try:
                    src_dir.rmdir()  # 비었을 때만 지워진다
                except OSError:
                    pass
            try:
                old_dir.rmdir()
            except OSError:
                pass
        if moved:
            log.info("원본 데이터 %d개를 %s -> %s 로 옮김", moved, old_dir, self.data_dir)
        return moved

    def close(self) -> None:
        with self._lock:
            self._conn.close()
