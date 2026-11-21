"""Parallel catch-up worker ('backfill agent') for exact missed stream footage when joining a live stream midway.

1. Detects the exact elapsed/missed stream time at the moment you joined (`missed_seconds`).
2. Scans each window using the lightweight `Audio_Only` stream (`-vn -ac 1 -ar 16000`, ~7s per window)
   to find the loudest 30-second reaction peak without downloading video.
3. Downloads ONLY the winning 30-second video slice using `-c copy` (~7s, zero re-encode during download),
   then renders the 9:16 clip with the KreekCraft logo overlay and Whisper captions.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

import captioner
import clipper
import config

# Keep catch-up scratch files in system temp (outside temp_recordings/ and OneDrive)
# so live spike extraction (`seg_*.ts`) and OneDrive file-sync never lock them.
BACKFILL_TEMP_DIR = Path(tempfile.gettempdir()) / "clipsmith_backfill"
BACKFILL_TEMP_DIR.mkdir(parents=True, exist_ok=True)


def safe_unlink(path: Path | None) -> None:
    """Remove a temporary file without raising WinError 32 if briefly held by another handle."""
    if path is None:
        return
    for _ in range(3):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.25)


def fmt_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def fmt_clock(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def resolve_backfill_source(platform: str, channel: str, stream_url: str, join_ts: float | None = None) -> dict | None:
    """Detect exact missed stream duration at join_ts and return audio + video DVR/VOD URLs."""
    import yt_dlp

    join_ts = join_ts or time.time()
    candidates = []
    if platform == "twitch" and channel:
        # Twitch publishes the live in-progress archive VOD while the stream is still running
        candidates.append(f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time")
    if stream_url:
        candidates.append(stream_url)
    if "kreek" in (channel or "").lower():
        candidates.append("https://www.twitch.tv/kreekcraft/videos?filter=archives&sort=time")
        candidates.append("https://www.youtube.com/@Kreek/streams")

    opts = {"quiet": True, "no_warnings": True, "playlistend": 1}
    for url in dict.fromkeys(candidates):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue
                if info.get("entries"):
                    entries = [e for e in info["entries"] if e]
                    if not entries:
                        continue
                    info = entries[0]

                fmts = info.get("formats") or []
                audio_url = next(
                    (f["url"] for f in fmts if f.get("url") and ("audio" in (f.get("format_id") or "").lower() or f.get("vcodec") == "none")),
                    None,
                ) or next(
                    (f["url"] for f in fmts if f.get("url") and f.get("height") in (160, 240, 360)),
                    None,
                )
                video_url = (
                    next((f["url"] for f in fmts if f.get("url") and f.get("height") == 720), None)
                    or next((f["url"] for f in fmts if f.get("url") and f.get("height") == 480), None)
                    or next((f["url"] for f in fmts if f.get("url") and f.get("height") == 1080), None)
                    or info.get("url")
                    or info.get("manifest_url")
                )
                if not video_url:
                    continue
                if not audio_url:
                    audio_url = video_url

                dur = float(info.get("duration") or 0.0)
                start_ts = info.get("release_timestamp") or info.get("timestamp")
                if dur <= 0 and start_ts:
                    dur = max(0.0, join_ts - float(start_ts))

                if dur > 30.0:
                    return {
                        "video_url": str(video_url),
                        "audio_url": str(audio_url),
                        "webpage_url": str(info.get("webpage_url") or url),
                        "missed_seconds": dur,
                        "started_at": float(start_ts) if start_ts else (join_ts - dur),
                        "title": info.get("title") or f"{channel} stream",
                    }
        except Exception:
            continue
    return None


def scan_window_for_best_30s(
    source_info: dict, start_sec: float, length_sec: float, wav_path: Path, clip_len: float = 30.0
) -> tuple[float, float]:
    """Download only the audio of [start_sec, start_sec + length_sec] (~7s) and return (best_offset, peak_score)."""
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.unlink(missing_ok=True)
    audio_url = source_info.get("audio_url") or source_info.get("video_url") or ""
    if not audio_url.startswith("http"):
        return 0.0, 0.0

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, start_sec):.2f}",
        "-t", f"{length_sec:.2f}",
        "-i", audio_url,
        "-vn", "-ac", "1", "-ar", "16000",
        str(wav_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if proc.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size < 4096:
            return 0.0, 0.0
        return score_wav_for_best_30s(wav_path, clip_len=clip_len)
    except Exception:
        return 0.0, 0.0
    finally:
        safe_unlink(wav_path)


def download_winning_30s_slice(
    source_info: dict, clip_start_sec: float, out_ts: Path, duration: float = 32.0
) -> bool:
    """Download ONLY the winning 30-second video slice with `-c copy` (~7s, zero CPU re-encode)."""
    out_ts.parent.mkdir(parents=True, exist_ok=True)
    safe_unlink(out_ts)
    video_url = source_info.get("video_url") or source_info.get("url") or ""

    if video_url.startswith("http") and not ("youtube.com/" in video_url or "twitch.tv/" in video_url):
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{max(0.0, clip_start_sec):.2f}",
            "-t", f"{duration:.2f}",
            "-i", video_url,
            "-c", "copy",
            str(out_ts),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if proc.returncode == 0 and out_ts.is_file() and out_ts.stat().st_size > 10_000:
            return True

    webpage = source_info.get("webpage_url") or video_url
    if webpage:
        end_sec = clip_start_sec + duration
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--quiet", "--no-warnings", "--no-playlist",
            "--live-from-start",
            "--download-sections", f"*{int(clip_start_sec)}-{int(end_sec)}",
            "-f", "bv*[height<=720]+ba/b[height<=720]/b",
            "--merge-output-format", "mp4",
            "-o", str(out_ts),
            webpage,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and out_ts.is_file() and out_ts.stat().st_size > 10_000:
            return True
    return False


def score_wav_for_best_30s(wav_path: Path, clip_len: float = 30.0) -> tuple[float, float]:
    """Return (best_offset_sec, peak_score) from a 16kHz mono WAV file."""
    with wave.open(str(wav_path), "rb") as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    sr = 16000
    n_secs = max(1, len(samples) // sr)
    if n_secs <= int(clip_len):
        rms_all = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
        return 0.0, rms_all

    trimmed = samples[: n_secs * sr].reshape(n_secs, sr)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1))
    win = min(int(clip_len), n_secs)
    best_sec = 0
    best_score = -1.0
    for s in range(0, max(1, n_secs - win + 1), 2):
        slice_rms = rms[s : s + win]
        score = float(np.mean(slice_rms) + 1.5 * np.max(slice_rms))
        if score > best_score:
            best_score = score
            best_sec = s
    return float(best_sec), best_score


def pick_best_30s_offset(video_path: Path, clip_len: float = 30.0) -> float:
    """Find the most exciting 30-second window in a local media file using audio energy."""
    try:
        total_dur = clipper.probe_duration(video_path)
    except Exception:
        return 0.0
    if total_dur <= clip_len + 2.0:
        return 0.0

    wav_path = video_path.with_suffix(".backfill.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(wav_path)],
            check=True, capture_output=True, text=True,
        )
        best_sec, _ = score_wav_for_best_30s(wav_path, clip_len=clip_len)
        return float(min(max(0.0, best_sec), max(0.0, total_dur - clip_len)))
    except Exception:
        return 0.0
    finally:
        wav_path.unlink(missing_ok=True)


def process_chunk_to_clip(
    chunk_path: Path,
    tag: str,
    vertical_mode: bool = True,
    vertical_layout: str = "blur",
    campaign_mode: bool = True,
    start_offset: float | None = None,
    overlay_path: Path | None = None,
    use_default_overlay: bool = True,
    campaign_rules: dict | None = None,
) -> Path:
    """Extract a 30s moment from chunk_path, apply logo overlay if campaign requires it, and burn cliffhanger + Whisper captions."""
    offset = pick_best_30s_offset(chunk_path, clip_len=30.0) if start_offset is None else start_offset
    raw_clip = BACKFILL_TEMP_DIR / f"raw_{tag}.mp4"
    out_clip = config.OUTPUT_DIR / f"{tag}.mp4"
    try:
        clipper.extract_clip(
            chunk_path,
            raw_clip,
            start_offset=offset,
            duration=30.0,
            vertical_mode=vertical_mode,
            vertical_layout=vertical_layout,
            overlay_path=overlay_path,
            use_default_overlay=use_default_overlay,
        )
        chan = tag.split("_")[0] if "_" in tag else "kreekcraft"
        captioner.caption_clip(
            raw_clip,
            out_clip,
            channel=chan,
            campaign_mode=campaign_mode,
            campaign_rules=campaign_rules,
        )
        return out_clip
    finally:
        safe_unlink(raw_clip)
