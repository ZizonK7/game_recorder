"""FFmpeg 기반 화면 녹화.

게임 성능에 영향을 최소화하기 위한 설계:
  * 화면 캡처: Desktop Duplication(ddagrab) - GPU 메모리에서 바로 프레임을 가져옴
  * 인코딩: 그래픽카드 하드웨어 인코더(NVENC / AMF / QuickSync) 우선, CPU(x264)는 마지막 대안
  * 축소(1080p -> 720p): 가능하면 GPU에서 처리 (scale_cuda / scale_qsv)
  * 프로세스 우선순위: '낮음(BELOW_NORMAL)' 으로 실행해 게임이 항상 먼저 CPU를 쓰게 함

녹화는 2초짜리 .ts 조각으로 저장한다. 덕분에
  * 죽었을 때 최근 조각만 이어 붙여 즉시 리플레이를 만들 수 있고
  * 프로그램이 비정상 종료돼도 이미 저장된 조각은 살아남는다.
게임이 끝나면 조각들을 재인코딩 없이(-c copy) 하나의 mp4로 합친다.
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SEGMENT_SECONDS = 2
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2

if os.name == "nt":
    CREATE_NO_WINDOW = 0x08000000
    BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
    LOW_PRIORITY_FLAGS = CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
else:
    LOW_PRIORITY_FLAGS = 0


def popen_low_priority(cmd: list[str], **kwargs) -> subprocess.Popen:
    if os.name == "nt":
        kwargs.setdefault("creationflags", LOW_PRIORITY_FLAGS)
    else:
        kwargs.setdefault("preexec_fn", lambda: os.nice(10))
    return subprocess.Popen(cmd, **kwargs)


def run_quiet(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    kwargs = {"creationflags": LOW_PRIORITY_FLAGS} if os.name == "nt" else {}
    return subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)


# --------------------------------------------------------------------------- 인코딩 파이프라인

@dataclass
class Pipeline:
    """ddagrab 이후의 필터 체인 + 인코더 옵션 조합."""

    name: str
    vf: str | None
    codec_args: list[str]


def even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def target_size(src_w: int, src_h: int, target_h: int) -> tuple[int, int] | None:
    """축소할 크기. 축소가 필요 없으면 None."""
    if not target_h or src_h <= target_h:
        return None
    return even(src_w * target_h / src_h), even(target_h)


def build_pipelines(encoder: str, fps: int, bitrate_kbps: int,
                    size: tuple[int, int] | None) -> list[Pipeline]:
    """선호 순서대로 시도할 파이프라인 목록."""
    b = f"{bitrate_kbps}k"
    rate = ["-b:v", b, "-maxrate", f"{int(bitrate_kbps * 1.5)}k", "-bufsize", f"{bitrate_kbps * 2}k"]
    gop = ["-g", str(fps * SEGMENT_SECONDS), "-bf", "0"]
    nvenc = ["-c:v", "h264_nvenc", "-preset", "p2", "-tune", "ll", "-rc", "vbr", *rate, *gop]
    qsv = ["-c:v", "h264_qsv", "-preset", "veryfast", *rate, *gop]
    amf = ["-c:v", "h264_amf", "-usage", "lowlatency", "-quality", "speed", "-rc", "vbr_peak", *rate, *gop]
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", *rate, *gop,
            "-pix_fmt", "yuv420p"]

    cpu_scale = "hwdownload,format=bgra" + (f",scale={size[0]}:{size[1]}:flags=bilinear" if size else "")

    out: dict[str, list[Pipeline]] = {
        "nvenc": [
            # 전부 GPU: D3D11 -> CUDA 매핑 후 GPU에서 축소
            Pipeline("nvenc-gpu",
                     f"hwmap=derive_device=cuda,scale_cuda={size[0]}:{size[1]}:format=nv12" if size else None,
                     nvenc),
            Pipeline("nvenc-cpuscale", cpu_scale + ",format=nv12", nvenc),
        ],
        "qsv": [
            Pipeline("qsv-gpu",
                     "hwmap=derive_device=qsv,format=qsv" + (f",scale_qsv=w={size[0]}:h={size[1]}" if size else ""),
                     qsv),
            Pipeline("qsv-cpuscale", cpu_scale + ",format=nv12", qsv),
        ],
        "amf": [
            # AMF는 D3D11 프레임을 바로 받을 수 있음 (축소가 필요 없을 때)
            *([Pipeline("amf-gpu", None, amf)] if not size else []),
            Pipeline("amf-cpuscale", cpu_scale + ",format=nv12", amf),
        ],
        "x264": [Pipeline("x264", cpu_scale + ",format=yuv420p", x264)],
    }
    order = ["nvenc", "amf", "qsv", "x264"] if encoder == "auto" else [encoder]
    if encoder != "auto" and encoder != "x264":
        order.append("x264")
    return [p for key in order for p in out.get(key, [])]


def ddagrab_input(monitor: int, fps: int) -> str:
    # draw_mouse=1: 리뷰할 때 커서 위치가 보이도록
    return f"ddagrab=output_idx={monitor}:framerate={fps}:draw_mouse=1"


def probe_pipeline(ffmpeg: Path, monitor: int, fps: int, pipeline: Pipeline, seconds: float = 1.5) -> tuple[bool, str]:
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", ddagrab_input(monitor, fps)]
    if pipeline.vf:
        cmd += ["-vf", pipeline.vf]
    cmd += [*pipeline.codec_args, "-t", str(seconds), "-f", "null", "-"]
    try:
        r = run_quiet(cmd, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as e:
        return False, str(e)
    return r.returncode == 0, r.stderr.decode(errors="ignore")[-500:]


def detect_pipeline(ffmpeg: Path, encoder: str, fps: int, bitrate: int, monitor: int,
                    size: tuple[int, int] | None) -> Pipeline | None:
    for p in build_pipelines(encoder, fps, bitrate, size):
        ok, err = probe_pipeline(ffmpeg, monitor, fps, p)
        log.info("인코더 테스트 %s: %s %s", p.name, "성공" if ok else "실패", "" if ok else err.strip())
        if ok:
            return p
    return None


# --------------------------------------------------------------------------- 조각 목록

@dataclass
class Segment:
    path: Path
    start: float  # 녹화 시작 기준 (초)
    end: float


def read_segments(seg_dir: Path, list_file: Path) -> list[Segment]:
    """segment 목록 CSV(완료된 조각만 기록됨)를 읽어 녹화 시작 기준 시간으로 정규화."""
    rows: list[tuple[str, float, float]] = []
    try:
        with list_file.open(newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 3:
                    try:
                        rows.append((row[0], float(row[1]), float(row[2])))
                    except ValueError:
                        continue
    except OSError:
        return []
    if not rows:
        return []
    base = rows[0][1]
    return [Segment(seg_dir / name, s - base, e - base) for name, s, e in rows]


def select_segments(segments: list[Segment], start: float, end: float) -> list[Segment]:
    return [s for s in segments if s.end > start and s.start < end]


# --------------------------------------------------------------------------- 녹화기

@dataclass
class RecorderConfig:
    ffmpeg: Path
    pipeline: Pipeline
    monitor: int = 0
    fps: int = 30
    audio_pipe: str | None = None  # 게임 소리 PCM 이 들어오는 named pipe


@dataclass
class _Progress:
    wall: float = 0.0
    out_time: float = 0.0
    started: bool = False


class FfmpegRecorder:
    def __init__(self, cfg: RecorderConfig, work_dir: Path):
        self.cfg = cfg
        self.work_dir = work_dir
        self.seg_dir = work_dir / "segments"
        self.list_file = self.seg_dir / "segments.csv"
        self.log_file = work_dir / "ffmpeg.log"
        self.proc: subprocess.Popen | None = None
        self._progress = _Progress()
        self._lock = threading.Lock()
        self.started_wall: float | None = None
        self._log_handle = None

    # ------------------------------------------------------------------ control
    def command(self) -> list[str]:
        c = self.cfg
        cmd = [str(c.ffmpeg), "-hide_banner", "-loglevel", "warning", "-nostats",
               "-progress", "pipe:1", "-stats_period", "0.5",
               "-f", "lavfi", "-i", ddagrab_input(c.monitor, c.fps)]
        if c.audio_pipe:
            cmd += ["-thread_queue_size", "4096", "-f", "s16le", "-ar", str(AUDIO_RATE),
                    "-ac", str(AUDIO_CHANNELS), "-i", c.audio_pipe]
        if c.pipeline.vf:
            cmd += ["-vf", c.pipeline.vf]
        cmd += c.pipeline.codec_args
        cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})"]
        cmd += ["-map", "0:v"]
        if c.audio_pipe:
            cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "160k"]
        cmd += ["-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mpegts",
                "-segment_list", str(self.list_file), "-segment_list_type", "csv",
                str(self.seg_dir / "seg_%05d.ts")]
        return cmd

    def start(self) -> None:
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        cmd = self.command()
        log.info("녹화 시작: %s", " ".join(cmd))
        self._log_handle = self.log_file.open("ab")
        self.proc = popen_low_priority(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=self._log_handle)
        self.started_wall = time.monotonic()
        threading.Thread(target=self._read_progress, daemon=True, name="ffmpeg-progress").start()

    def _read_progress(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode(errors="ignore").strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                with self._lock:
                    self._progress = _Progress(time.monotonic(), us / 1_000_000, True)

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def video_time_now(self) -> float | None:
        """지금 이 순간이 영상의 몇 초 지점인지 추정."""
        with self._lock:
            p = self._progress
        if not p.started:
            return None
        return p.out_time + (time.monotonic() - p.wall)

    def segments(self) -> list[Segment]:
        return read_segments(self.seg_dir, self.list_file)

    def stop(self, timeout: float = 10.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                assert self.proc.stdin
                self.proc.stdin.write(b"q\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg 가 제때 종료되지 않아 강제 종료합니다")
                self.proc.kill()
                self.proc.wait()
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        log.info("녹화 종료 (exit=%s)", self.proc.returncode)


# --------------------------------------------------------------------------- 조각 합치기

def concat_quote(path: Path) -> str:
    """concat 목록 파일용 따옴표 처리. 작은따옴표는 '\\'' 로 바꿔야 한다 (예: player's recordings)."""
    return "'" + path.as_posix().replace("'", "'\\''") + "'"


def concat_segments(ffmpeg: Path, segments: list[Path], output: Path, timeout: float = 600) -> bool:
    """조각들을 재인코딩 없이 하나의 mp4로 합친다."""
    segments = [s for s in segments if s.exists() and s.stat().st_size > 0]
    if not segments:
        return False
    list_path = output.with_suffix(".concat.txt")
    with list_path.open("w", encoding="utf-8") as f:
        for s in segments:
            f.write(f"file {concat_quote(s)}\n")
    tmp = output.with_name(output.stem + ".part.mp4")
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
           "-i", str(list_path), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(tmp)]
    try:
        r = run_quiet(cmd, timeout=timeout)
        ok = r.returncode == 0 and tmp.exists()
        if not ok:
            # 오디오가 없는 녹화에서는 bsf 옵션이 실패할 수 있어 한 번 더 시도
            cmd.remove("-bsf:a")
            cmd.remove("aac_adtstoasc")
            r = run_quiet(cmd, timeout=timeout)
            ok = r.returncode == 0 and tmp.exists()
        if ok:
            tmp.replace(output)
        else:
            log.error("조각 합치기 실패: %s", r.stderr.decode(errors="ignore")[-500:])
        return ok
    except subprocess.TimeoutExpired:
        log.error("조각 합치기 시간 초과")
        return False
    finally:
        list_path.unlink(missing_ok=True)


def probe_duration(ffmpeg: Path, video: Path) -> float | None:
    ffprobe = ffmpeg.with_name(ffmpeg.name.replace("ffmpeg", "ffprobe"))
    if ffprobe.exists():
        r = run_quiet([str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=nw=1:nk=1", str(video)], timeout=30)
        try:
            return float(r.stdout.decode().strip())
        except ValueError:
            pass
    # ffprobe 가 없으면 ffmpeg 출력에서 Duration 파싱
    r = run_quiet([str(ffmpeg), "-hide_banner", "-i", str(video)], timeout=30)
    import re

    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr.decode(errors="ignore"))
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return None


@dataclass
class ReplayClip:
    path: Path
    seek: float  # 클립 안에서 재생을 시작할 위치
    length: float
    extra: dict = field(default_factory=dict)


def build_replay_clip(ffmpeg: Path, segments: list[Segment], death_video_time: float,
                      before: float, after: float, output: Path) -> ReplayClip | None:
    start, end = death_video_time - before, death_video_time + after
    chosen = select_segments(segments, start, end)
    if not chosen:
        return None
    if not concat_segments(ffmpeg, [s.path for s in chosen], output, timeout=30):
        return None
    seek = max(0.0, start - chosen[0].start)
    return ReplayClip(output, seek, end - max(start, chosen[0].start))
