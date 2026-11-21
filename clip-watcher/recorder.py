"""Live Twitch and YouTube stream recorder using streamlink/yt-dlp and segmented ffmpeg output."""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import config

_SEG_TS = re.compile(r"seg_(\d{8}_\d{6})\.ts$")


def _normalize_target_url(target: str) -> str:
    text = (target or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text
    if any(text.lower().startswith(p) for p in ("twitch.tv/", "www.twitch.tv/", "youtube.com/", "www.youtube.com/", "youtu.be/")):
        return f"https://{text}"
    return f"https://www.twitch.tv/{text.lstrip('#@').lower()}"


def resolve_stream_url(target: str) -> str | None:
    """Resolve the live HLS/manifest URL for an active Twitch or YouTube Live stream, or None if offline/VOD."""
    url = _normalize_target_url(target)
    if not url:
        return None

    # 1. For YouTube URLs, check live_status first so an ended stream archive (VOD) is handled by the Parallel Catch-Up Agent instead of live segmented recording
    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        try:
            import yt_dlp

            opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": "best"}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info.get("entries"):
                    entries = [e for e in info["entries"] if e]
                    info = entries[0] if entries else {}
                if not (info.get("is_live") or info.get("live_status") == "is_live"):
                    return None
                direct = info.get("url") or info.get("manifest_url")
                if direct and str(direct).startswith("http"):
                    return str(direct)
        except Exception:
            pass

    # 2. Try streamlink (Twitch Live and YouTube Live fallback)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "streamlink", "--stream-url", url, "best"],
            capture_output=True, text=True, timeout=25,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            stream_url = proc.stdout.strip().splitlines()[-1].strip()
            if stream_url.startswith("http"):
                return stream_url
    except Exception:
        pass
    return None


def segment_time_bounds(path: Path, prev_end: float | None, default_len: float) -> tuple[float, float]:
    """Return approximate (start_ts, end_ts) epoch seconds for a recorded .ts segment."""
    end_ts = path.stat().st_mtime
    m = _SEG_TS.search(path.name)
    if m:
        start_ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp()
        return start_ts, max(end_ts, start_ts)
    start_ts = prev_end if prev_end is not None else end_ts - default_len
    return min(start_ts, end_ts), end_ts


def cleanup_old_segments(
    temp_dir: Path, retention_seconds: float, now: float | None = None
) -> list[Path]:
    """Delete seg_*.ts segments older than retention_seconds, keeping the newest active segment."""
    now = time.time() if now is None else now
    segments = sorted(temp_dir.glob("seg_*.ts"), key=lambda p: (p.stat().st_mtime, p.name))
    if len(segments) <= 1:
        return []
    cutoff = now - retention_seconds
    removed: list[Path] = []
    for seg in segments[:-1]:
        try:
            if seg.stat().st_mtime < cutoff:
                seg.unlink(missing_ok=True)
                removed.append(seg)
        except OSError:
            continue
    return removed


def get_segments_for_window(
    temp_dir: Path, center_ts: float, pre_sec: float = 15.0, post_sec: float = 15.0
) -> list[Path]:
    """Return sorted seg_*.ts segments overlapping [center_ts - pre_sec, center_ts + post_sec]."""
    win_start = center_ts - pre_sec
    win_end = center_ts + post_sec
    segments = sorted(temp_dir.glob("seg_*.ts"), key=lambda p: (p.stat().st_mtime, p.name))
    matched: list[Path] = []
    prev_end: float | None = None
    for seg in segments:
        s_ts, e_ts = segment_time_bounds(seg, prev_end, config.SEGMENT_SECONDS)
        prev_end = e_ts
        if e_ts >= win_start - 1.0 and s_ts <= win_end + 1.0:
            matched.append(seg)
    return matched


class StreamRecorder:
    """Polls a Twitch or YouTube Live stream and records into rolling MPEG-TS segments."""

    def __init__(
        self,
        channel: str = config.TWITCH_CHANNEL,
        stream_target: str | None = None,
        temp_dir: Path = config.TEMP_DIR,
        segment_seconds: int = config.SEGMENT_SECONDS,
        retention_seconds: int = config.RETENTION_SECONDS,
        poll_interval: float = 15.0,
    ) -> None:
        self.channel = channel
        self.stream_target = stream_target or channel
        self.temp_dir = temp_dir
        self.segment_seconds = segment_seconds
        self.retention_seconds = retention_seconds
        self.poll_interval = poll_interval
        self.is_live = False
        self.started_at: float | None = None
        self.last_error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._stop_event = asyncio.Event()

    def active_segment_count(self) -> int:
        return len(list(self.temp_dir.glob("seg_*.ts")))

    def _start_ffmpeg(self, hls_url: str) -> None:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        out_pattern = str(self.temp_dir / "seg_%Y%m%d_%H%M%S.ts")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", hls_url,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.segment_seconds),
            "-strftime", "1",
            "-reset_timestamps", "1",
            out_pattern,
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        self.is_live = True
        self.started_at = time.time()
        self.last_error = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self.is_live = False
        self.started_at = None

    async def run(self) -> None:
        """Continuously poll for stream availability and maintain segmented recording."""
        try:
            while not self._stop_event.is_set():
                cleanup_old_segments(self.temp_dir, self.retention_seconds)
                if self._proc is not None:
                    rc = self._proc.poll()
                    if rc is None:
                        await self._sleep(1.0)
                        continue
                    stderr = self._proc.stderr.read().strip() if self._proc.stderr else ""
                    if rc != 0 and stderr:
                        self.last_error = stderr[-200:]
                    self._proc = None
                    self.is_live = False
                    self.started_at = None

                try:
                    hls_url = await asyncio.to_thread(resolve_stream_url, self.stream_target)
                except Exception as exc:
                    self.last_error = str(exc)
                    hls_url = None

                if hls_url:
                    self._start_ffmpeg(hls_url)
                    await self._sleep(1.0)
                else:
                    await self._sleep(self.poll_interval)
        finally:
            self.stop()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
