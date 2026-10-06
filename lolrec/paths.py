"""앱 데이터/리소스 경로."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME


def app_data_dir() -> Path:
    """설정, DB, 로그가 저장되는 위치 (%APPDATA%/LoLRecorder)."""
    base = os.environ.get("APPDATA")
    if base:
        path = Path(base) / APP_NAME
    else:
        path = Path.home() / ".local" / "share" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_recordings_dir() -> Path:
    """기본 녹화 저장 위치 (내 동영상/LoLRecorder)."""
    return Path.home() / "Videos" / APP_NAME


def resource_dir() -> Path:
    """exe에 번들된 리소스(ffmpeg, 오디오 헬퍼)가 있는 위치."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _find_binary(name: str) -> Path | None:
    candidates = [
        resource_dir() / "bin" / name,
        resource_dir() / "vendor" / name,
        Path(sys.executable).parent / "bin" / name,
    ]
    for c in candidates:
        if c.exists():
            return c
    from shutil import which

    found = which(name)
    return Path(found) if found else None


def ffmpeg_path() -> Path | None:
    return _find_binary("ffmpeg.exe" if os.name == "nt" else "ffmpeg")


def audio_helper_path() -> Path | None:
    return _find_binary("lol_audio_capture.exe")
