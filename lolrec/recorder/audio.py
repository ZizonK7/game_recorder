"""게임 소리만 녹음하는 헬퍼(lol_audio_capture.exe) 실행기.

Windows 10 2004 이상의 'Process Loopback' 기능으로 League of Legends.exe 의 소리만 캡처한다.
디스코드, 음악, 마이크 등 다른 소리는 들어가지 않는다.
헬퍼는 named pipe 를 만들어 PCM(48kHz, 16bit, 스테레오)을 흘려보내고, FFmpeg 이 그 pipe 를 읽는다.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import uuid
from pathlib import Path

from .ffmpeg import AUDIO_CHANNELS, AUDIO_RATE, popen_low_priority

log = logging.getLogger(__name__)


class GameAudioCapture:
    def __init__(self, helper: Path, pid: int):
        self.helper = helper
        self.pid = pid
        self.pipe_name = rf"\\.\pipe\lolrec_audio_{uuid.uuid4().hex[:8]}"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 5.0) -> bool:
        """헬퍼를 띄우고 pipe 가 준비될 때까지 대기. 실패하면 False (영상만 녹화)."""
        cmd = [str(self.helper), "--pid", str(self.pid), "--pipe", self.pipe_name,
               "--rate", str(AUDIO_RATE), "--channels", str(AUDIO_CHANNELS)]
        try:
            self.proc = popen_low_priority(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            log.error("오디오 헬퍼 실행 실패: %s", e)
            return False

        lines: queue.Queue[str] = queue.Queue()

        def reader():
            assert self.proc and self.proc.stdout
            for raw in self.proc.stdout:
                line = raw.decode(errors="ignore").strip()
                if line:
                    log.info("[audio] %s", line)
                    lines.put(line)

        threading.Thread(target=reader, daemon=True, name="audio-helper-log").start()
        try:
            while True:
                line = lines.get(timeout=timeout)
                if line.startswith("READY"):
                    return True
                if line.startswith("ERROR"):
                    self.stop()
                    return False
        except queue.Empty:
            log.error("오디오 헬퍼 응답 없음")
            self.stop()
            return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
