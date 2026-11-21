"""Orchestrator and live Rich terminal dashboard for clip-watcher."""
from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

import captioner
import clipper
import config
from chat_monitor import SpikeEvent, TwitchChatMonitor
from recorder import StreamRecorder


def _fmt_uptime(started_at: float | None) -> str:
    if not started_at:
        return "-"
    secs = max(0, int(time.time() - started_at))
    hrs, rem = divmod(secs, 3600)
    mins, s = divmod(rem, 60)
    return f"{hrs:02d}:{mins:02d}:{s:02d}"


class ClipWatcherApp:
    """Coordinates stream recording, chat spike monitoring, and clip rendering."""

    def __init__(self, channel: str = config.TWITCH_CHANNEL) -> None:
        self.channel = channel
        self.spike_queue: asyncio.Queue[SpikeEvent] = asyncio.Queue()
        self.recorder = StreamRecorder(channel=channel)
        self.chat = TwitchChatMonitor(channel=channel, on_spike=self._on_spike)
        self.clips_exported = 0
        self.last_clip_name = "-"
        self.status_note = "Watching stream & chat..."
        self._stop_event = asyncio.Event()

    async def _on_spike(self, event: SpikeEvent) -> None:
        if not self.recorder.is_live:
            self.status_note = f"Spike ignored ({event.count} msgs/10s): stream offline"
            return
        self.status_note = (
            f"Spike queued ({event.count} msgs/10s, {event.multiplier}x) - waiting {int(config.POST_SPIKE_WAIT_SECONDS)}s"
        )
        await self.spike_queue.put(event)

    async def _clip_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = await asyncio.wait_for(self.spike_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                stamp = datetime.fromtimestamp(event.timestamp).strftime("%Y%m%d_%H%M%S")
                raw_path = config.TEMP_DIR / f"raw_{self.channel}_{stamp}.mp4"
                out_path = config.OUTPUT_DIR / f"{self.channel}_{stamp}.mp4"
                self.status_note = f"Extracting 30s clip for spike at {stamp}..."
                await asyncio.to_thread(
                    clipper.extract_spike_from_buffer,
                    event.timestamp,
                    config.TEMP_DIR,
                    raw_path,
                )
                self.status_note = f"Transcribing & burning captions into {out_path.name}..."
                await asyncio.to_thread(captioner.caption_clip, raw_path, out_path)
                raw_path.unlink(missing_ok=True)
                self.clips_exported += 1
                self.last_clip_name = out_path.name
                self.status_note = f"Exported {out_path.name}"
            except Exception as exc:
                self.status_note = f"Clip error: {exc}"
            finally:
                self.spike_queue.task_done()

    def build_dashboard(self) -> Panel:
        table = Table(expand=True, show_header=False, box=None)
        table.add_column("Metric", style="bold cyan", width=22)
        table.add_column("Value")

        stream_badge = "[bold green]LIVE[/]" if self.recorder.is_live else "[bold yellow]OFFLINE[/]"
        irc_badge = "[green]connected[/]" if self.chat.connected else "[yellow]connecting[/]"
        baseline = self.chat.detector.baseline
        threshold = max(config.MIN_SPIKE_MESSAGES, round(baseline * config.SPIKE_MULTIPLIER, 1))
        last_spike = self.chat.detector.spikes[-1] if self.chat.detector.spikes else None
        spike_text = (
            f"{len(self.chat.detector.spikes)} "
            f"(last: {last_spike.count} msgs, {last_spike.multiplier}x)"
            if last_spike
            else "0"
        )

        table.add_row("Channel", f"#{self.channel}  (IRC: {irc_badge})")
        table.add_row(
            "Stream Status",
            f"{stream_badge}  | Uptime: {_fmt_uptime(self.recorder.started_at)}  "
            f"| Segments: {self.recorder.active_segment_count()}",
        )
        table.add_row(
            "Live Chat Rate",
            f"{self.chat.current_count} msgs (cur) / {self.chat.last_window_count} msgs/10s  "
            f"| Baseline: {baseline:.1f}  | Trigger >= {threshold}",
        )
        table.add_row("Detected Spikes", spike_text)
        table.add_row("Clips Exported", f"{self.clips_exported}  | Latest: {self.last_clip_name}")
        table.add_row("Status", self.status_note)
        return Panel(table, title="clip-watcher", border_style="bright_blue")

    async def run(self, duration: float | None = None) -> None:
        tasks = [
            asyncio.create_task(self.recorder.run()),
            asyncio.create_task(self.chat.run()),
            asyncio.create_task(self._clip_worker()),
        ]
        console = Console()
        start = time.time()
        try:
            with Live(self.build_dashboard(), console=console, refresh_per_second=2) as live:
                while not self._stop_event.is_set():
                    live.update(self.build_dashboard())
                    if duration is not None and (time.time() - start) >= duration:
                        break
                    await asyncio.sleep(0.5)
        finally:
            self._stop_event.set()
            self.recorder.stop()
            self.chat.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor a Twitch stream and export hyped clips.")
    parser.add_argument("--channel", default=config.TWITCH_CHANNEL, help="Twitch channel name")
    parser.add_argument("--check", action="store_true", help="Render dashboard briefly and exit")
    parser.add_argument("--duration", type=float, default=None, help="Run for N seconds then exit")
    args = parser.parse_args()

    app = ClipWatcherApp(channel=args.channel.lstrip("#").lower())
    if args.check:
        Console().print(app.build_dashboard())
        return

    try:
        asyncio.run(app.run(duration=args.duration))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

