"""End-to-end simulation test for spike detection, segment cleanup, cutting, and Whisper captions."""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import captioner
import clipper
import config
from chat_monitor import SpikeDetector
from recorder import cleanup_old_segments


def test_spike_detector_and_cooldown() -> None:
    detector = SpikeDetector(multiplier=2.5, min_messages=10, cooldown_seconds=60.0)
    t = 1000.0
    for count in (5, 6, 5, 7, 6, 5):
        t += 10.0
        assert detector.record_window(count, now=t) is None, f"Unexpected spike on baseline {count}"

    spike = detector.record_window(25, now=t + 10.0)
    assert spike is not None, "Expected spike on 25 messages"
    assert spike.count == 25 and spike.multiplier >= 2.5

    suppressed = detector.record_window(28, now=t + 30.0)
    assert suppressed is None, "Expected spike to be suppressed during cooldown"

    second = detector.record_window(30, now=t + 80.0)
    assert second is not None, "Expected spike after cooldown elapsed"
    print("[PASS] Spike detection & cooldown")


def test_segment_cleanup() -> None:
    tmp = config.TEMP_DIR / "_test_cleanup"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        now = time.time()
        old_seg = tmp / "seg_01.ts"
        recent_seg = tmp / "seg_02.ts"
        active_seg = tmp / "seg_03.ts"
        for p, age in ((old_seg, 400), (recent_seg, 60), (active_seg, 5)):
            p.write_bytes(b"ts")
            ts = now - age
            import os
            os.utime(p, (ts, ts))

        removed = cleanup_old_segments(tmp, retention_seconds=300, now=now)
        assert removed == [old_seg], f"Expected only old_seg removed, got {removed}"
        assert not old_seg.exists() and recent_seg.exists() and active_seg.exists()
        print("[PASS] Rolling segment cleanup")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _create_sample_stream_segments(seg_dir: Path) -> list[Path]:
    """Synthesize a 36s 1280x720 video with spoken audio and split it into .ts segments."""
    seg_dir.mkdir(parents=True, exist_ok=True)
    speech_wav = seg_dir / "speech.wav"
    ps_cmd = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{str(speech_wav)}'); "
        "$s.Speak('Oh my goodness, what an incredible clutch play right there! "
        "Chat is going completely crazy over that shot!'); "
        "$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=True)

    seg_pattern = str(seg_dir / "seg_%03d.ts")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=36",
            "-stream_loop", "-1", "-i", str(speech_wav),
            "-t", "36", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "segment", "-segment_time", "6", "-reset_timestamps", "1", seg_pattern,
        ],
        check=True,
    )
    return sorted(seg_dir.glob("seg_*.ts"))


def _probe_video(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    w, h = map(int, out[0].split(",")[:2])
    dur = float(out[-1].strip().rstrip(","))
    return w, h, dur


def test_end_to_end_clip_and_captions() -> None:
    seg_dir = config.TEMP_DIR / "_test_segments"
    shutil.rmtree(seg_dir, ignore_errors=True)
    raw_clip = config.TEMP_DIR / "_test_raw_30s.mp4"
    out_mp4 = config.OUTPUT_DIR / "test_spike_clip.mp4"
    out_srt = config.OUTPUT_DIR / "test_spike_clip.srt"

    try:
        segments = _create_sample_stream_segments(seg_dir)
        assert len(segments) >= 5, f"Expected >= 5 segments, got {len(segments)}"

        clipper.extract_clip(
            segments,
            raw_clip,
            start_offset=3.0,
            duration=30.0,
            vertical_mode=True,
            vertical_layout="blur",
        )
        assert raw_clip.exists()

        captioner.caption_clip(raw_clip, out_mp4, model_name=config.WHISPER_MODEL, srt_path=out_srt)
        assert out_mp4.exists(), "Final captioned MP4 was not created"
        assert out_srt.exists(), "SRT subtitle file was not created"

        srt_text = out_srt.read_text(encoding="utf-8")
        assert "-->" in srt_text, f"Expected SRT cues, got:\n{srt_text}"

        w, h, dur = _probe_video(out_mp4)
        assert (w, h) == (1080, 1920), f"Expected 1080x1920 vertical output, got {w}x{h}"
        assert 29.0 <= dur <= 31.0, f"Expected ~30s clip duration, got {dur:.2f}s"
        print(f"[PASS] End-to-end 30s 9:16 cut + Whisper SRT + burn-in ({w}x{h}, {dur:.2f}s)")
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
        raw_clip.unlink(missing_ok=True)


if __name__ == "__main__":
    test_spike_detector_and_cooldown()
    test_segment_cleanup()
    test_end_to_end_clip_and_captions()
    print("All clip-watcher pipeline checks passed.")

