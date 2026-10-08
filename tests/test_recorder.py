"""녹화 파이프라인 테스트.

실제 화면 캡처(ddagrab)는 Windows 에서만 되므로, 여기서는 ffmpeg 의 테스트 영상(testsrc)으로
조각 저장 -> 조각 목록 -> 데스 리플레이 클립 -> 최종 합치기 흐름을 검증한다.
"""

import shutil
import time
from pathlib import Path

import pytest

from lolrec.recorder import ffmpeg as rec


def test_target_size():
    assert rec.target_size(1920, 1080, 720) == (1280, 720)
    assert rec.target_size(2560, 1440, 720) == (1280, 720)
    assert rec.target_size(3440, 1440, 720) == (1720, 720)
    assert rec.target_size(1280, 720, 720) is None
    assert rec.target_size(1920, 1080, 0) is None


def test_build_pipelines_order_and_options():
    ps = rec.build_pipelines("auto", 30, 5000, (1280, 720))
    names = [p.name for p in ps]
    assert names[0] == "nvenc-gpu" and names[-1] == "x264"
    assert "amf-gpu" not in names  # 축소가 필요하면 AMF는 CPU 축소 사용
    nv = ps[0]
    assert "scale_cuda=1280:720" in nv.vf
    assert nv.codec_args[nv.codec_args.index("-g") + 1] == "60"
    only_amf = [p.name for p in rec.build_pipelines("amf", 60, 5000, None)]
    assert only_amf == ["amf-gpu", "amf-cpuscale", "x264"]
    # AMF 는 forced_idr 가 없으면 2초 키프레임이 생기지 않아 조각이 나뉘지 않는다
    for p in rec.build_pipelines("amf", 30, 5000, None)[:2]:
        assert p.codec_args[p.codec_args.index("-forced_idr") + 1] == "1"
    assert rec.build_pipelines("nvenc", 30, 5000, None)[0].vf is None


def test_read_and_select_segments(tmp_path):
    lst = tmp_path / "segments.csv"
    lst.write_text("seg_00000.ts,1.4,3.4\nseg_00001.ts,3.4,5.4\nseg_00002.ts,5.4,7.4\nbad\n")
    segs = rec.read_segments(tmp_path, lst)
    assert [round(s.start, 2) for s in segs] == [0.0, 2.0, 4.0]
    assert [s.path.name for s in rec.select_segments(segs, 3.0, 4.5)] == ["seg_00001.ts", "seg_00002.ts"]
    assert rec.read_segments(tmp_path, tmp_path / "missing.csv") == []


FFMPEG = shutil.which("ffmpeg")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 없음")
def test_segment_record_replay_and_concat(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "ddagrab_input", lambda monitor, fps: f"testsrc2=size=640x360:rate={fps}")
    pipeline = rec.Pipeline("test-x264", "format=yuv420p",
                            ["-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-bf", "0"])
    r = rec.FfmpegRecorder(rec.RecorderConfig(Path(FFMPEG), pipeline, fps=15), tmp_path)
    cmd = r.command()
    assert "-f" in cmd and "segment" in cmd and "-force_key_frames" in cmd
    r.start()
    # testsrc 는 실시간보다 빠르게 생성되므로 진행 상황(progress)이 한 번 보고될 때까지 대기
    deadline = time.time() + 20
    while time.time() < deadline and (len(r.segments()) < 4 or r.video_time_now() is None):
        time.sleep(0.2)
    assert r.video_time_now() is not None
    r.stop()
    segs = r.segments()
    assert len(segs) >= 4
    assert abs(segs[1].start - rec.SEGMENT_SECONDS) < 0.2

    clip = rec.build_replay_clip(Path(FFMPEG), segs, death_video_time=5.0, before=3.0, after=1.0,
                                 output=tmp_path / "death.mp4")
    assert clip and clip.path.exists()
    assert 0 <= clip.seek < rec.SEGMENT_SECONDS + 0.01
    assert abs(clip.length - 4.0) < 0.01

    out = tmp_path / "video.mp4"
    assert rec.concat_segments(Path(FFMPEG), sorted(r.seg_dir.glob("seg_*.ts")), out)
    dur = rec.probe_duration(Path(FFMPEG), out)
    assert dur is not None and dur >= segs[-1].end - 0.5


@pytest.mark.skipif(FFMPEG is None or not hasattr(__import__("os"), "mkfifo"), reason="ffmpeg/mkfifo 없음")
def test_record_with_audio_pipe(tmp_path, monkeypatch):
    """Windows named pipe 대신 FIFO 로 오디오 헬퍼를 흉내내 오디오 트랙 포함 녹화를 검증."""
    import os
    import threading

    monkeypatch.setattr(rec, "ddagrab_input", lambda monitor, fps: f"testsrc2=size=320x180:rate={fps}")
    fifo = tmp_path / "audio.pipe"
    os.mkfifo(fifo)
    stop = threading.Event()

    def helper():
        chunk = b"\x00\x00" * rec.AUDIO_CHANNELS * (rec.AUDIO_RATE // 50)  # 20ms 무음
        try:
            with open(fifo, "wb", buffering=0) as f:
                while not stop.is_set():
                    f.write(chunk)
                    time.sleep(0.02)
        except (BrokenPipeError, ValueError):
            pass  # ffmpeg 가 끝나면 pipe 가 닫힘 (실제 헬퍼도 이때 종료)

    t = threading.Thread(target=helper, daemon=True)
    t.start()
    pipeline = rec.Pipeline("test-x264", "format=yuv420p", ["-c:v", "libx264", "-preset", "ultrafast"])
    r = rec.FfmpegRecorder(rec.RecorderConfig(Path(FFMPEG), pipeline, fps=15, audio_pipe=str(fifo)), tmp_path)
    r.start()
    time.sleep(3)
    r.stop()
    stop.set()
    out = tmp_path / "video.mp4"
    assert rec.concat_segments(Path(FFMPEG), sorted(r.seg_dir.glob("seg_*.ts")), out)
    info = rec.run_quiet([FFMPEG, "-hide_banner", "-i", str(out)]).stderr.decode()
    assert "Audio: aac" in info and "Video: h264" in info


def test_concat_quote():
    assert rec.concat_quote(Path("/a/b.ts")) == "'/a/b.ts'"
    assert rec.concat_quote(Path("/player's recordings/seg.ts")) == r"'/player'\''s recordings/seg.ts'"


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 없음")
def test_concat_in_folder_with_quote(tmp_path):
    folder = tmp_path / "player's recordings"
    folder.mkdir()
    segs = []
    for i in range(2):
        seg = folder / f"seg_{i:05d}.ts"
        rec.run_quiet([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                       "testsrc2=size=160x90:rate=10", "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
                       "-f", "mpegts", str(seg)], timeout=30)
        assert seg.exists()
        segs.append(seg)
    out = folder / "video.mp4"
    assert rec.concat_segments(Path(FFMPEG), segs, out)
    assert out.exists()
