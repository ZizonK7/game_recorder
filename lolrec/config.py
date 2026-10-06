"""사용자 설정 (settings.json) 과 API 키 보관 (Windows 자격 증명 관리자)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import APP_NAME
from .paths import app_data_dir, default_recordings_dir

log = logging.getLogger(__name__)

KEYRING_SERVICE = APP_NAME
KEYRING_USER = "riot_api_key"

# 해상도 프리셋: 이름 -> 세로 픽셀 (0 = 원본)
RESOLUTIONS = {"720p": 720, "900p": 900, "1080p": 1080, "원본": 0}
ENCODERS = ["auto", "nvenc", "amf", "qsv", "x264"]
RANKED_QUEUES = {420, 440}  # 솔로랭크, 자유랭크


@dataclass
class Settings:
    # 녹화
    recordings_dir: str = str(default_recordings_dir())
    resolution: str = "720p"
    fps: int = 30
    bitrate_kbps: int = 5000
    encoder: str = "auto"
    monitor_index: int = 0
    record_game_audio: bool = True
    ranked_only: bool = False
    max_storage_gb: float = 100.0
    keep_recent_videos: int = 0  # 최근 N경기 영상만 보관 (0 = 끄기). 경기 데이터는 유지

    # 데스 리플레이
    death_replay_enabled: bool = True
    replay_before_sec: float = 8.0
    replay_after_sec: float = 2.0
    replay_overlay_position: str = "top-center"  # top-left/top-center/top-right/bottom-left/bottom-right
    replay_overlay_scale: float = 0.35  # 화면 너비 대비
    replay_close_on_respawn: bool = True
    replay_volume: float = 0.0  # 게임 소리와 겹치지 않도록 기본 음소거

    # Riot API
    region: str = "auto"  # auto, KR, JP1, NA1, EUW1 ...
    riot_id: str = ""  # "이름#태그" (비워두면 클라이언트에서 자동 감지)
    puuid: str = ""

    # 재생
    seek_lead_sec: float = 5.0  # 이벤트 클릭 시 몇 초 전부터 재생할지

    # 앱
    start_minimized: bool = False
    launch_on_startup: bool = False

    # 내부 캐시 (인코더 자동 탐지 결과)
    detected_pipeline: dict = field(default_factory=dict)
    raw_data_dir: str = ""  # 마지막으로 원본 JSON 을 저장한 위치 (저장 위치 변경 시 이관용)
    raw_data_fallbacks: list = field(default_factory=list)  # 옮기지 못한 파일이 남은 예전 위치들

    @property
    def recordings_path(self) -> Path:
        p = Path(self.recordings_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def raw_data_path(self) -> Path:
        """원본 API JSON 위치: 녹화 저장 위치 아래 data 폴더."""
        return self.recordings_path / "data"

    @property
    def target_height(self) -> int:
        return RESOLUTIONS.get(self.resolution, 720)

    def should_record_queue(self, queue_id: int | None) -> bool:
        if not self.ranked_only:
            return True
        # 큐 정보를 못 얻으면 일단 녹화 (나중에 놓친 판이 더 아쉬움)
        return queue_id is None or queue_id in RANKED_QUEUES


def settings_file() -> Path:
    return app_data_dir() / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_file()
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("설정 파일을 읽지 못해 기본값을 사용합니다")
        return Settings()
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in data.items() if k in known})


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_api_key() -> str:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception:  # keyring 백엔드가 없는 환경
        log.warning("자격 증명 저장소를 사용할 수 없습니다")
        return ""


def set_api_key(key: str) -> None:
    import keyring

    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass
