"""경기/녹화 메타데이터를 SQLite에 저장. 원본 API JSON은 파일로 따로 둔다."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

RAW_SUBDIRS = ("matches", "timelines")


@dataclass
class MoveResult:
    moved: int = 0
    failed: list[Path] = field(default_factory=list)
    unreachable: list[Path] = field(default_factory=list)  # 지금은 접근할 수 없는 예전 위치 (드라이브 분리 등)


def read_json(path: Path) -> dict | None:
    """JSON 파일을 읽는다. 없거나 손상됐으면 None."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("원본 데이터 파일을 읽지 못함 (손상?): %s", path, exc_info=True)
        return None


def copy_verified(src: Path, dst: Path) -> None:
    """src 를 임시 파일로 복사하고 크기와 JSON 형식을 확인한 뒤에만 dst 로 교체한다.

    중간에 실패하면(디스크 부족 등) dst 는 건드리지 않고 OSError 를 낸다.
    """
    tmp = dst.with_name(dst.name + ".part")
    try:
        shutil.copy2(src, tmp)
        if tmp.stat().st_size != src.stat().st_size or read_json(tmp) is None:
            raise OSError(f"복사본 검증 실패: {dst}")
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

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
        # 아직 옮기지 못한 파일이 남아 있는 예전 원본 JSON 위치 (읽기 전용으로 계속 찾아봄)
        self.fallback_dirs: list[Path] = []
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
    def _raw_path(self, sub: str, name: str) -> Path:
        """원본 JSON 경로. 현재 위치를 먼저 보고, 없거나 손상됐으면 아직 옮기지 못한 예전 위치를 본다."""
        primary = self.data_dir / sub / name
        candidates = [primary, *(d / sub / name for d in self.fallback_dirs)]
        existing = [c for c in candidates if c.exists()]
        for c in existing:
            if read_json(c) is not None:
                return c
        return existing[0] if existing else primary

    def match_json_path(self, match_id: str) -> Path:
        return self._raw_path("matches", f"{match_id}.json")

    def timeline_json_path(self, match_id: str) -> Path:
        return self._raw_path("timelines", f"{match_id}_timeline.json")

    def save_raw(self, match_id: str, match: dict | None, timeline: dict | None) -> None:
        with self._data_lock:
            for sub, name, data in (("matches", f"{match_id}.json", match),
                                    ("timelines", f"{match_id}_timeline.json", timeline)):
                if data is None:
                    continue
                p = self.data_dir / sub / name  # 새 데이터는 항상 현재 위치에
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(p)

    def load_match(self, match_id: str) -> dict | None:
        """경기 원본 JSON. 정상 파일이 어디에도 없으면 None (손상된 파일 때문에 오류를 내지 않음)."""
        with self._data_lock:
            return read_json(self.match_json_path(match_id))

    def load_timeline(self, match_id: str) -> dict | None:
        with self._data_lock:
            return read_json(self.timeline_json_path(match_id))

    def delete_raw(self, match_id: str) -> None:
        with self._data_lock:
            for d in (self.data_dir, *self.fallback_dirs):
                (d / "matches" / f"{match_id}.json").unlink(missing_ok=True)
                (d / "timelines" / f"{match_id}_timeline.json").unlink(missing_ok=True)

    def relocate_data(self, new_dir: Path) -> MoveResult:
        """원본 JSON 저장 위치를 바꾸고 기존 파일을 새 위치로 옮긴다.

        일부를 옮기지 못하면 예전 위치를 fallback_dirs 에 남겨 계속 읽을 수 있게 한다.
        """
        with self._data_lock:
            old = self.data_dir
            new_dir.mkdir(parents=True, exist_ok=True)
            self.data_dir = new_dir
            if old not in self.fallback_dirs:
                self.fallback_dirs.insert(0, old)
            result = self.import_data_from(old)
            # 다른 예전 위치에 남아 있던 파일도 이번에 다시 시도
            for d in list(self.fallback_dirs):
                if d != old:
                    other = self.import_data_from(d)
                    result.moved += other.moved
                    result.failed += other.failed
                    result.unreachable += other.unreachable
            return result

    def import_data_from(self, old_dir: Path) -> MoveResult:
        """다른 위치에 남아 있는 원본 JSON 을 현재 위치로 옮긴다.

        - 파일마다 임시 파일로 복사 -> 크기/JSON 검증 -> 교체한 뒤에만 원본을 지운다.
        - 새 위치에 이미 정상 파일이 있으면 그대로 두고, 손상된 파일이면 원본으로 다시 복사한다.
        - 모두 옮겼으면 fallback_dirs 에서 빼고, 하나라도 못 옮겼으면 남긴다.
        - 기억하던 위치에 지금 접근할 수 없으면(드라이브 분리 등) 이관 완료로 보지 않고 남겨 둔다.
        """
        result = MoveResult()
        with self._data_lock:
            try:
                if old_dir.resolve() == self.data_dir.resolve():
                    self._drop_fallback(old_dir)
                    return result
                reachable = old_dir.exists()
            except OSError:
                reachable = False
            if not reachable:
                if old_dir in self.fallback_dirs:
                    log.warning("예전 원본 데이터 위치에 접근할 수 없어 다음에 다시 시도: %s", old_dir)
                    result.unreachable.append(old_dir)
                return result
            for sub in RAW_SUBDIRS:
                src_dir, dst_dir = old_dir / sub, self.data_dir / sub
                try:
                    if not src_dir.is_dir():
                        continue
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    files = list(src_dir.glob("*.json"))
                except OSError:
                    log.warning("원본 데이터 폴더를 읽지 못함: %s", src_dir, exc_info=True)
                    result.failed.append(src_dir)
                    continue
                for f in files:
                    dst = dst_dir / f.name
                    if dst.exists() and read_json(dst) is not None:
                        continue  # 이미 새 위치에 정상 파일이 있음 (예전 파일은 그대로 둠)
                    if read_json(f) is None:
                        log.warning("예전 위치의 원본 데이터가 손상되어 옮기지 않음: %s", f)
                        continue
                    try:
                        copy_verified(f, dst)
                    except OSError:
                        log.warning("원본 데이터 이동 실패: %s", f, exc_info=True)
                        result.failed.append(f)
                        continue
                    result.moved += 1
                    try:
                        f.unlink()
                    except OSError:  # 새 위치의 복사본은 검증됐으므로 원본은 남아도 된다
                        log.warning("옮긴 원본 데이터의 예전 파일을 지우지 못함: %s", f)
                try:
                    src_dir.rmdir()  # 비었을 때만 지워진다
                except OSError:
                    pass
            try:
                old_dir.rmdir()
            except OSError:
                pass
            if result.failed:
                if old_dir not in self.fallback_dirs:
                    self.fallback_dirs.append(old_dir)
            else:
                self._drop_fallback(old_dir)
        if result.moved:
            log.info("원본 데이터 %d개를 %s -> %s 로 옮김", result.moved, old_dir, self.data_dir)
        if result.failed:
            log.warning("원본 데이터 %d개를 옮기지 못해 예전 위치에서 계속 읽음: %s", len(result.failed), old_dir)
        return result

    def _drop_fallback(self, d: Path) -> None:
        if d in self.fallback_dirs:
            self.fallback_dirs.remove(d)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
