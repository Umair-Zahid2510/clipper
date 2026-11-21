"""Anonymous Twitch IRC and YouTube Live chat monitor with rolling 10-second spike detector."""
from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

import requests

import config

TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6667

_YT_MSG_ID_RE = re.compile(r'"clientMessageId"\s*:\s*"([^"]+)"|"id"\s*:\s*"(LCC\.[^"]+)"')


@dataclass
class SpikeEvent:
    timestamp: float
    count: int
    baseline: float
    multiplier: float


class SpikeDetector:
    """Detects hype spikes across 10-second message windows with cooldown suppression."""

    def __init__(
        self,
        multiplier: float = config.SPIKE_MULTIPLIER,
        min_messages: int = config.MIN_SPIKE_MESSAGES,
        cooldown_seconds: float = config.COOLDOWN_SECONDS,
        history_windows: int = 24,
    ) -> None:
        self.multiplier = multiplier
        self.min_messages = min_messages
        self.cooldown_seconds = cooldown_seconds
        self.history: deque[int] = deque(maxlen=history_windows)
        self.last_spike_ts = float("-inf")
        self.spikes: list[SpikeEvent] = []

    @property
    def baseline(self) -> float:
        return sum(self.history) / len(self.history) if self.history else 0.0

    def record_window(self, count: int, now: float | None = None) -> SpikeEvent | None:
        """Record one 10-second message count and return a SpikeEvent if triggered."""
        now = time.time() if now is None else now
        eff_baseline = (
            self.baseline
            if len(self.history) >= 3 and self.baseline > 0
            else (float(self.min_messages) / self.multiplier)
        )
        triggered = (
            count >= self.min_messages
            and count >= eff_baseline * self.multiplier
            and (now - self.last_spike_ts) >= self.cooldown_seconds
        )
        self.history.append(count)
        if not triggered:
            return None

        ratio = (count / eff_baseline) if eff_baseline > 0 else float(self.multiplier)
        event = SpikeEvent(
            timestamp=now,
            count=count,
            baseline=round(eff_baseline, 2),
            multiplier=round(ratio, 2),
        )
        self.last_spike_ts = now
        self.spikes.append(event)
        return event


def _poll_youtube_live_chat_ids(video_id: str) -> list[str]:
    """Fetch visible live chat item IDs from YouTube's public live_chat page."""
    url = f"https://www.youtube.com/live_chat?v={video_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code != 200:
        return []
    return [m.group(1) or m.group(2) for m in _YT_MSG_ID_RE.finditer(resp.text)]


class TwitchChatMonitor:
    """Monitors Twitch IRC and/or YouTube Live chat and measures chat rate in 10s intervals."""

    def __init__(
        self,
        channel: str = config.TWITCH_CHANNEL,
        detector: SpikeDetector | None = None,
        on_spike: Callable[[SpikeEvent], Awaitable[None] | None] | None = None,
        window_seconds: float = 10.0,
        platform: str = "twitch",
        yt_video_id: str | None = None,
    ) -> None:
        self.channel = channel.lstrip("#@").lower()
        self.platform = platform
        self.yt_video_id = yt_video_id
        self.detector = detector or SpikeDetector()
        self.on_spike = on_spike
        self.window_seconds = window_seconds
        self.current_count = 0
        self.last_window_count = 0
        self.total_messages = 0
        self.connected = False
        self.last_error: str | None = None
        self._stop_event = asyncio.Event()
        self._seen_yt_ids: deque[str] = deque(maxlen=600)

    def stop(self) -> None:
        self._stop_event.set()

    async def _irc_loop(self, irc_channel: str) -> None:
        while not self._stop_event.is_set():
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(TWITCH_IRC_HOST, TWITCH_IRC_PORT), timeout=15
                )
                writer.write(b"PASS SCHMOOPIIE\r\n")
                writer.write(b"NICK justinfan12345\r\n")
                writer.write(f"JOIN #{irc_channel}\r\n".encode("utf-8"))
                await writer.drain()
                self.connected = True
                self.last_error = None

                while not self._stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="ignore").rstrip("\r\n")
                    if line.startswith("PING"):
                        writer.write(b"PONG :tmi.twitch.tv\r\n")
                        await writer.drain()
                    elif " PRIVMSG " in line:
                        self.current_count += 1
                        self.total_messages += 1
            except Exception as exc:
                self.last_error = str(exc)
            finally:
                if self.platform == "twitch":
                    self.connected = False
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
            if not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

    async def _youtube_loop(self, video_id: str) -> None:
        first_poll = True
        while not self._stop_event.is_set():
            try:
                ids = await asyncio.to_thread(_poll_youtube_live_chat_ids, video_id)
                self.connected = True
                new_count = 0
                for mid in ids:
                    if mid not in self._seen_yt_ids:
                        self._seen_yt_ids.append(mid)
                        new_count += 1
                if not first_poll and new_count > 0:
                    self.current_count += new_count
                    self.total_messages += new_count
                first_poll = False
            except Exception as exc:
                self.last_error = str(exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def _ticker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.window_seconds)
                break
            except asyncio.TimeoutError:
                pass
            count = self.current_count
            self.current_count = 0
            self.last_window_count = count
            event = self.detector.record_window(count)
            if event and self.on_spike:
                res = self.on_spike(event)
                if inspect.isawaitable(res):
                    await res

    async def run(self) -> None:
        loops = [self._ticker_loop()]
        if self.platform == "youtube" and self.yt_video_id:
            loops.append(self._youtube_loop(self.yt_video_id))
            # If watching KreekCraft on YouTube Live, also mirror #kreekcraft Twitch IRC chat hype
            if "kreek" in self.channel:
                loops.append(self._irc_loop("kreekcraft"))
        else:
            irc_chan = "kreekcraft" if "kreek" in self.channel else self.channel
            loops.append(self._irc_loop(irc_chan))
        await asyncio.gather(*loops)
