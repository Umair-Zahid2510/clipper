"""Web-dashboard bridge, YouTube + Twitch Live parsing, KreekCraft campaign rules, and logo overlay controller."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from . import campaigns, config as app_config

_CW_DIR = str(app_config.ROOT / "clip-watcher")
if _CW_DIR not in sys.path:
    sys.path.insert(0, _CW_DIR)

import backfill  # noqa: E402
import captioner  # noqa: E402
import clipper  # noqa: E402
import config as cw_config  # noqa: E402
import test_pipeline  # noqa: E402
from chat_monitor import SpikeDetector, SpikeEvent, TwitchChatMonitor  # noqa: E402
from recorder import StreamRecorder  # noqa: E402

OUTPUT_DIR: Path = cw_config.OUTPUT_DIR
ASSETS_DIR: Path = cw_config.ASSETS_DIR

_CHANNEL_RE = re.compile(r"^[a-z0-9_]{2,25}$")
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.mp4$")
_OVERLAY_EXTS = {".png", ".webp", ".gif", ".mov", ".mp4"}
_RESERVED = {"directory", "downloads", "jobs", "login", "settings", "signup", "videos"}
_meta_lock = threading.Lock()


class _ShortCaption(BaseModel):
    title: str = Field(description="Accurate, punchy YouTube Shorts title reflecting what happened, max 70 characters")
    description: str = Field(description="Accurate 1-sentence caption plus Watch KreekCraft on YouTube and Twitch, NO URLs")
    hashtags: list[str] = Field(description="Hashtags without #, including GTA6, KreekCraft, GrandTheftAutoVI")


def parse_stream_info(raw: str) -> dict:
    """Parse a Twitch or YouTube Live URL (or channel name) into platform, channel, stream_url, yt_video_id."""
    text = (raw or "").strip()
    if not text:
        raise ValueError(
            "Paste a YouTube Live link (https://www.youtube.com/live/...) or Twitch link (https://www.twitch.tv/kreekcraft)."
        )

    low = text.lower()
    if "://" in text or any(d in low for d in ("twitch.tv", "youtube.com", "youtu.be")):
        parsed = urlparse(text if "://" in text else f"https://{text}")
        host = (parsed.hostname or "").lower()
        parts = [p for p in parsed.path.split("/") if p]

        # YouTube Live URLs: /live/<id>, /watch?v=<id>, /@Handle/streams, /@Handle/live, youtu.be/<id>
        if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
            vid = None
            handle = None
            if host == "youtu.be" and parts:
                vid = parts[0]
            elif parts and parts[0] == "live" and len(parts) >= 2:
                vid = parts[1]
            elif parts and parts[0] == "watch":
                vid = (parse_qs(parsed.query).get("v") or [None])[0]
            elif parts and parts[0].startswith("@"):
                handle = parts[0].lstrip("@").lower()

            if vid and _YT_ID_RE.fullmatch(vid):
                is_kreek = vid == "dy-oCN2NxfA" or "kreek" in low
                is_speed = vid == "qSyo5d-dVzA" or "speed" in low
                chan = "kreekcraft" if is_kreek else ("ishowspeed" if is_speed else f"yt_{vid[:10].lower()}")
                return {
                    "platform": "youtube",
                    "channel": chan,
                    "stream_url": f"https://www.youtube.com/live/{vid}",
                    "yt_video_id": vid,
                }
            if handle:
                is_kreek = "kreek" in handle
                chan = "kreekcraft" if is_kreek else re.sub(r"[^a-z0-9_]", "", handle)[:25] or "youtube_live"
                vid_guess = "dy-oCN2NxfA" if is_kreek else None
                return {
                    "platform": "youtube",
                    "channel": chan,
                    "stream_url": f"https://www.youtube.com/@{handle}/live",
                    "yt_video_id": vid_guess,
                }
            raise ValueError(
                "Paste a valid YouTube Live link (e.g. https://www.youtube.com/live/dy-oCN2NxfA or https://www.youtube.com/@Kreek/streams)."
            )

        # Twitch URLs: twitch.tv/<channel> or twitch.tv/<channel>/videos
        if host == "twitch.tv" or host.endswith(".twitch.tv"):
            chan = parts[0].lower() if parts else ""
            if not _CHANNEL_RE.fullmatch(chan) or chan in _RESERVED:
                raise ValueError("Paste a valid Twitch stream link (like https://www.twitch.tv/kreekcraft).")
            return {
                "platform": "twitch",
                "channel": chan,
                "stream_url": f"https://www.twitch.tv/{chan}",
                "yt_video_id": None,
            }

        raise ValueError("Only YouTube Live (youtube.com) and Twitch (twitch.tv) stream links are supported.")

    chan = text.lstrip("@#").lower()
    if chan == "kreek":
        chan = "kreekcraft"
    if not _CHANNEL_RE.fullmatch(chan) or chan in _RESERVED:
        raise ValueError(
            "Paste a YouTube Live link (https://www.youtube.com/live/dy-oCN2NxfA) or Twitch link (https://www.twitch.tv/kreekcraft)."
        )
    return {
        "platform": "twitch",
        "channel": chan,
        "stream_url": f"https://www.twitch.tv/{chan}",
        "yt_video_id": None,
    }


def parse_stream_target(raw: str) -> str:
    """Return the normalized channel login for a Twitch or YouTube Live target."""
    return parse_stream_info(raw)["channel"]


def evaluate_campaign_compliance(
    title: str,
    description: str,
    hashtags: list[str],
    add_source_link: bool,
    overlay_applied: bool,
    campaign_rules: dict | None = None,
) -> dict:
    """Check a clip's metadata and overlay against the active campaign profile rules."""
    rules = campaign_rules or campaigns.get_campaign()
    if rules.get("id") == "general":
        return {
            "compliant": True,
            "overlay_ok": True,
            "cta_ok": True,
            "tags_ok": True,
            "no_links_ok": True,
            "campaign_name": "General Mode",
        }
    desc_low = (description or "").lower()
    req_cta = (rules.get("required_cta") if "required_cta" in rules else "watch kreekcraft on youtube and twitch").strip()
    # Compare core CTA words (strip trailing emojis/punctuation)
    core_cta = re.sub(r"[^\w\s]+$", "", req_cta).strip().lower()
    cta_ok = (not core_cta) or (core_cta in desc_low)
    tag_set = {str(t).lstrip("#").strip().lower() for t in (hashtags or [])}
    required_tags = {str(t).lstrip("#").strip().lower() for t in (rules.get("required_hashtags") or []) if str(t).strip()}
    tags_ok = (not required_tags) or required_tags.issubset(tag_set) or all(f"#{r}" in desc_low for r in required_tags)
    if rules.get("no_links", True):
        has_url = bool(captioner._URL_RE.search(description or "")) or bool(captioner._URL_RE.search(title or "")) or bool(add_source_link)
        no_links_ok = not has_url
    else:
        no_links_ok = True
    if rules.get("require_overlay", True):
        overlay_ok = bool(overlay_applied or campaigns.get_overlay_path_for_campaign(rules.get("id")) is not None or cw_config.get_overlay_path() is not None)
    else:
        overlay_ok = True
    return {
        "compliant": bool(cta_ok and tags_ok and no_links_ok and overlay_ok),
        "overlay_ok": overlay_ok,
        "cta_ok": cta_ok,
        "tags_ok": tags_ok,
        "no_links_ok": no_links_ok,
        "campaign_name": rules.get("name"),
    }


class WatcherController:
    """Manages active YouTube/Twitch stream monitoring, KreekCraft campaign rules, and clip exports."""

    def __init__(self) -> None:
        self.running = False
        self.platform: str = "youtube"
        self.channel: str | None = "kreekcraft"
        self.stream_url: str | None = cw_config.KREEKCRAFT_YT_LIVE
        self.yt_video_id: str | None = "dy-oCN2NxfA"
        self.campaign_mode: bool = cw_config.CAMPAIGN_MODE
        self.multiplier = cw_config.SPIKE_MULTIPLIER
        self.min_messages = cw_config.MIN_SPIKE_MESSAGES
        self.vertical_mode = cw_config.VERTICAL_MODE
        self.vertical_layout = cw_config.VERTICAL_LAYOUT
        self.auto_post = False
        self.auto_privacy = "public"
        self.auto_account_id: str | None = None
        self.backfill_enabled = True
        self.backfill_missed_seconds = 0.0
        self.backfill_chunk_minutes = 10
        self.backfill_running = False
        self.backfill_status = "Idle"
        self.backfill_clips_count = 0
        self.on_clip_exported = None
        self.recorder: StreamRecorder | None = None
        self.chat: TwitchChatMonitor | None = None
        self.busy = False
        self.status_note = "Idle"
        self._tasks: list[asyncio.Task] = []
        self._manual_task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._spike_queue: asyncio.Queue[SpikeEvent] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._recover_interrupted_uploads()

    def active_campaign(self) -> dict:
        return campaigns.get_campaign()

    def overlay_status(self) -> dict:
        camp = self.active_campaign()
        p = campaigns.get_overlay_path_for_campaign(camp.get("id")) or cw_config.get_overlay_path()
        return {
            "present": p is not None,
            "filename": p.name if p else None,
            "url": f"/watcher-assets/{p.name}" if p else None,
            "campaign_id": camp.get("id"),
            "campaign_name": camp.get("name"),
            "required": bool(camp.get("require_overlay", True)),
        }

    def save_overlay(self, filename: str, content: bytes, campaign_id: str | None = None) -> dict:
        ext = Path(filename or "").suffix.lower()
        if ext not in _OVERLAY_EXTS:
            raise ValueError("Upload a PNG, WebP, GIF, MOV, or MP4 logo overlay file.")
        if not content:
            raise ValueError("Uploaded overlay file is empty.")
        cid = campaign_id or self.active_campaign().get("id") or "kreekcraft-gta6"
        if cid != "general":
            campaigns.save_campaign_overlay(cid, filename, content)
        cw_config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        for old in cw_config.ASSETS_DIR.glob("logo_overlay.*"):
            old.unlink(missing_ok=True)
        dest = cw_config.ASSETS_DIR / f"logo_overlay{ext}"
        dest.write_bytes(content)
        return self.overlay_status()

    def remove_overlay(self) -> dict:
        for old in cw_config.ASSETS_DIR.glob("logo_overlay.*"):
            old.unlink(missing_ok=True)
        return self.overlay_status()

    def _recover_interrupted_uploads(self) -> None:
        for meta_path in cw_config.OUTPUT_DIR.glob("*.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                changed = False
                for plat in ("youtube", "tiktok", "instagram"):
                    pinfo = data.get(plat)
                    if isinstance(pinfo, dict) and pinfo.get("status") in ("queued", "uploading"):
                        data[plat] = {
                            "status": "error",
                            "error": "The app was closed during the upload. It will resume automatically when the loop runs.",
                            "retryable": True,
                        }
                        changed = True
                if changed:
                    meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, ValueError):
                continue

    def resolve_clip_path(self, name: str) -> Path:
        if not isinstance(name, str) or not _CLIP_NAME_RE.fullmatch(name):
            raise ValueError("Not a valid clip filename.")
        root = os.path.realpath(cw_config.OUTPUT_DIR)
        target = Path(os.path.realpath(os.path.join(root, name)))
        if not str(target).startswith(root + os.sep):
            raise ValueError("Not a valid clip filename.")
        if not target.is_file():
            raise FileNotFoundError("Clip not found.")
        return target

    def read_meta(self, mp4_path: Path) -> dict:
        meta_path = mp4_path.with_suffix(".json")
        with _meta_lock:
            if meta_path.is_file():
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                    # Upgrade pre-campaign metadata if campaign_mode is active and clip hasn't been edited
                    if self.campaign_mode and not data.get("campaign_mode"):
                        upgraded = captioner.build_default_metadata(
                            mp4_path.with_suffix(".srt"), channel=self.channel, campaign_mode=True
                        )
                        upgraded["youtube"] = data.get("youtube")
                        self._write_meta_unlocked(mp4_path, upgraded)
                        return upgraded
                    return data
                except (OSError, ValueError):
                    pass
            data = captioner.build_default_metadata(
                mp4_path.with_suffix(".srt"), channel=self.channel, campaign_mode=self.campaign_mode
            )
            self._write_meta_unlocked(mp4_path, data)
            return data

    def _write_meta_unlocked(self, mp4_path: Path, data: dict) -> None:
        meta_path = mp4_path.with_suffix(".json")
        tmp = meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(meta_path)

    def update_meta(self, name: str, **fields) -> dict:
        target = self.resolve_clip_path(name)
        with _meta_lock:
            meta_path = target.with_suffix(".json")
            data = {}
            if meta_path.is_file():
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
            if not data:
                data = captioner.build_default_metadata(
                    target.with_suffix(".srt"), channel=self.channel, campaign_mode=self.campaign_mode
                )
            if self.campaign_mode:
                if "title" in fields:
                    fields["title"] = captioner.strip_urls(fields["title"])
                if "description" in fields:
                    fields["description"] = captioner.strip_urls(fields["description"])
                fields["add_source_link"] = False
            data.update(fields)
            self._write_meta_unlocked(target, data)
            return data

    def set_youtube(self, name: str, value: dict) -> None:
        try:
            self.update_meta(name, youtube=value)
        except FileNotFoundError:
            pass

    def generate_ai_metadata(self, name: str) -> dict:
        """Generate or polish Shorts title, description, and hashtags adhering to campaign rules."""
        target = self.resolve_clip_path(name)
        meta = self.read_meta(target)
        srt_path = target.with_suffix(".srt")
        transcript = meta.get("transcript") or captioner.extract_srt_transcript(srt_path)
        chan = meta.get("channel") or self.channel or "kreekcraft"
        base = captioner.build_default_metadata(srt_path, channel=chan, campaign_mode=self.campaign_mode)

        provider = app_config.ai_provider()
        if provider and transcript:
            sys_prompt = (
                "You write accurate, high-retention short-form titles and captions for KreekCraft's official GTA 6 streams. "
                "STRICT RULES:\n"
                "1. Never fabricate context or use misleading clickbait; stay 100% true to the transcript.\n"
                "2. Include 'Watch KreekCraft on YouTube and Twitch! 🔴' in the description.\n"
                "3. Do NOT include any links or URLs anywhere.\n"
                "4. Always include hashtags: GTA6, KreekCraft, GrandTheftAutoVI."
            )
            user_prompt = (
                f"Streamer: KreekCraft (GTA 6 livestream)\n"
                f"Spoken transcript:\n{transcript}\n\n"
                "Return JSON matching the schema."
            )
            try:
                if provider == "ollama":
                    from . import local_llm

                    try:
                        raw = local_llm.chat_json(sys_prompt, user_prompt, _ShortCaption.model_json_schema())
                        parsed = _ShortCaption.model_validate_json(raw)
                    finally:
                        local_llm.unload()
                elif provider == "gemini":
                    from google import genai

                    client = genai.Client()
                    res = client.interactions.create(
                        model=app_config.GEMINI_MODEL,
                        input=f"{sys_prompt}\n\n{user_prompt}",
                        response_format={
                            "type": "text",
                            "mime_type": "application/json",
                            "schema": _ShortCaption.model_json_schema(),
                        },
                    )
                    parsed = _ShortCaption.model_validate_json(res.output_text)
                else:
                    import anthropic

                    client = anthropic.Anthropic()
                    res = client.messages.parse(
                        model=app_config.CLAUDE_MODEL,
                        max_tokens=1024,
                        system=sys_prompt,
                        messages=[{"role": "user", "content": user_prompt}],
                        output_format=_ShortCaption,
                    )
                    parsed = res.parsed_output
                if parsed:
                    title = captioner.strip_urls(parsed.title).strip()[:100]
                    desc = captioner.strip_urls(parsed.description).strip()
                    if self.campaign_mode and "watch kreekcraft on youtube and twitch" not in desc.lower():
                        desc = f"{desc}\n\n{cw_config.CAMPAIGN_CTA}".strip()
                    tags = [h.lstrip("#").strip() for h in parsed.hashtags if h.strip()]
                    if self.campaign_mode:
                        for req_tag in reversed(cw_config.CAMPAIGN_HASHTAGS):
                            if req_tag.lower() not in [t.lower() for t in tags]:
                                tags.insert(0, req_tag)
                    updated = self.update_meta(
                        name,
                        title=title or base["title"],
                        description=desc or base["description"],
                        hashtags=tags[:5],
                    )
                    return {
                        "title": updated["title"],
                        "description": updated["description"],
                        "hashtags": updated["hashtags"],
                        "source": provider,
                    }
            except Exception:
                pass

        updated = self.update_meta(
            name,
            title=base["title"],
            description=base["description"],
            hashtags=base["hashtags"],
        )
        return {
            "title": updated["title"],
            "description": updated["description"],
            "hashtags": updated["hashtags"],
            "source": "transcript",
        }

    async def start(
        self,
        url: str,
        multiplier: float = 2.5,
        min_messages: int = 10,
        vertical_mode: bool = True,
        vertical_layout: str = "blur",
        auto_post: bool | None = None,
        auto_privacy: str | None = None,
        auto_account_id: str | None = None,
        campaign_mode: bool | None = None,
        backfill_enabled: bool | None = None,
        backfill_hours: float | None = None,
    ) -> dict:
        if vertical_layout not in ("blur", "crop"):
            raise ValueError("Unknown framing layout.")
        info = parse_stream_info(url)
        async with self._lock:
            await self._stop_unlocked()
            for seg in cw_config.TEMP_DIR.glob("*.ts"):
                seg.unlink(missing_ok=True)
            self.platform = info["platform"]
            self.channel = info["channel"]
            self.stream_url = info["stream_url"]
            self.yt_video_id = info["yt_video_id"]
            self.multiplier = float(multiplier)
            self.min_messages = int(min_messages)
            self.vertical_mode = bool(vertical_mode)
            self.vertical_layout = vertical_layout
            if campaign_mode is not None:
                self.campaign_mode = bool(campaign_mode)
            if auto_post is not None:
                self.auto_post = bool(auto_post)
            if auto_privacy in ("public", "unlisted", "private"):
                self.auto_privacy = auto_privacy
            if auto_account_id is not None:
                self.auto_account_id = auto_account_id or None
            if backfill_enabled is not None:
                self.backfill_enabled = bool(backfill_enabled)

            detector = SpikeDetector(
                multiplier=self.multiplier,
                min_messages=self.min_messages,
                cooldown_seconds=cw_config.COOLDOWN_SECONDS,
            )
            self.recorder = StreamRecorder(channel=self.channel, stream_target=self.stream_url)
            self.chat = TwitchChatMonitor(
                channel=self.channel,
                detector=detector,
                on_spike=self._on_spike,
                platform=self.platform,
                yt_video_id=self.yt_video_id,
            )
            self._spike_queue = asyncio.Queue()
            self.running = True
            plat_label = "YouTube Live" if self.platform == "youtube" else "Twitch"
            self.status_note = f"Monitoring #{self.channel} ({plat_label}) stream & chat..."
            self._tasks = [
                asyncio.create_task(self.recorder.run()),
                asyncio.create_task(self.chat.run()),
                asyncio.create_task(self._clip_worker()),
            ]
            if self.backfill_enabled:
                self._backfill_task = asyncio.create_task(self._run_backfill(join_ts=time.time()))
            return self.status()

    async def stop(self) -> dict:
        async with self._lock:
            await self._stop_unlocked()
            return self.status()

    async def _stop_unlocked(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
        if self.chat is not None:
            self.chat.stop()
        tasks = list(self._tasks)
        if self._manual_task is not None and not self._manual_task.done():
            tasks.append(self._manual_task)
        if self._backfill_task is not None and not self._backfill_task.done():
            tasks.append(self._backfill_task)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = []
        self._manual_task = None
        self._backfill_task = None
        self.backfill_running = False
        self.backfill_status = "Idle"
        while not self._spike_queue.empty():
            try:
                self._spike_queue.get_nowait()
                self._spike_queue.task_done()
            except asyncio.QueueEmpty:
                break
        self.running = False
        self.busy = False
        self.status_note = "Idle"

    def stop_sync(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
        if self.chat is not None:
            self.chat.stop()

    async def _on_spike(self, event: SpikeEvent) -> None:
        if not self.recorder or not self.recorder.is_live:
            self.status_note = f"Spike detected ({event.count} msgs/10s), waiting for stream to go live"
            return
        self.status_note = (
            f"Spike queued ({event.count} msgs/10s, {event.multiplier}x) — capturing post-reaction"
        )
        await self._spike_queue.put(event)

    async def _clip_worker(self) -> None:
        while True:
            event = await self._spike_queue.get()
            self.busy = True
            stamp = datetime.fromtimestamp(event.timestamp).strftime("%Y%m%d_%H%M%S")
            raw_path = cw_config.TEMP_DIR / f"raw_{self.channel}_{stamp}.mp4"
            out_path = cw_config.OUTPUT_DIR / f"{self.channel}_{stamp}.mp4"
            try:
                camp = self.active_campaign()
                is_camp = camp.get("id") != "general" and self.campaign_mode
                ov_path = campaigns.get_overlay_path_for_campaign(camp.get("id")) if is_camp else None
                self.status_note = f"Extracting 30s clip for spike at {stamp}..."
                await asyncio.to_thread(
                    clipper.extract_spike_from_buffer,
                    event.timestamp,
                    cw_config.TEMP_DIR,
                    raw_path,
                    vertical_mode=self.vertical_mode,
                    vertical_layout=self.vertical_layout,
                    overlay_path=ov_path,
                    use_default_overlay=is_camp,
                )
                self.status_note = f"Burning cliffhanger hook & captions into {out_path.name}..."
                await asyncio.to_thread(
                    captioner.caption_clip,
                    raw_path,
                    out_path,
                    channel=self.channel,
                    campaign_mode=is_camp,
                    campaign_rules=camp,
                )
                self.status_note = f"Exported {out_path.name}"
                if self.auto_post and self.on_clip_exported:
                    try:
                        self.on_clip_exported(out_path.name)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status_note = f"Clip error: {exc}"
            finally:
                raw_path.unlink(missing_ok=True)
                self.busy = False
                self._spike_queue.task_done()

    async def trigger_clip_now(self) -> dict:
        if self.busy or (self._manual_task is not None and not self._manual_task.done()):
            raise RuntimeError("A clip export is already running.")
        self.busy = True
        self.status_note = "Preparing 30s clip..."
        self._manual_task = asyncio.create_task(self._run_manual_clip())
        return self.status()

    async def _run_manual_clip(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = self.channel or "kreekcraft"
        raw_path = cw_config.TEMP_DIR / f"raw_{tag}_{stamp}.mp4"
        out_path = cw_config.OUTPUT_DIR / f"{tag}_{stamp}.mp4"
        sample_dir: Path | None = None
        try:
            segs = sorted(cw_config.TEMP_DIR.glob("seg_*.ts"), key=lambda p: (p.stat().st_mtime, p.name))
            # Exclude a segment currently being written (< 1.5s old) if enough completed segments exist
            if len(segs) > 2 and (time.time() - segs[-1].stat().st_mtime) < 1.5:
                segs = segs[:-1]
            if segs:
                chosen = segs[-7:]
                offset = 0.0
                self.status_note = f"Extracting 30s from #{self.channel} buffered stream segments..."
            else:
                sample_dir = cw_config.TEMP_DIR / f"_sample_{stamp}"
                self.status_note = "Stream offline — generating 36s sample buffer..."
                chosen = await asyncio.to_thread(test_pipeline._create_sample_stream_segments, sample_dir)
                offset = 3.0
                self.status_note = "Cutting 30s clip from sample stream..."

            camp = self.active_campaign()
            is_camp = camp.get("id") != "general" and self.campaign_mode
            ov_path = campaigns.get_overlay_path_for_campaign(camp.get("id")) if is_camp else None
            await asyncio.to_thread(
                clipper.extract_clip,
                chosen,
                raw_path,
                start_offset=offset,
                duration=30.0,
                vertical_mode=self.vertical_mode,
                vertical_layout=self.vertical_layout,
                overlay_path=ov_path,
                use_default_overlay=is_camp,
            )
            self.status_note = f"Transcribing & burning cliffhanger + captions into {out_path.name}..."
            await asyncio.to_thread(
                captioner.caption_clip,
                raw_path,
                out_path,
                channel=self.channel,
                campaign_mode=is_camp,
                campaign_rules=camp,
            )
            self.status_note = f"Exported {out_path.name}"
            if self.auto_post and self.on_clip_exported:
                try:
                    self.on_clip_exported(out_path.name)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status_note = f"Clip error: {exc}"
        finally:
            raw_path.unlink(missing_ok=True)
            if sample_dir is not None:
                shutil.rmtree(sample_dir, ignore_errors=True)
            self.busy = False

    async def start_backfill_now(self, url: str | None = None, hours: float | None = None) -> dict:
        if self.backfill_running or (self._backfill_task is not None and not self._backfill_task.done()):
            raise RuntimeError("Parallel catch-up agent is already running.")
        if url:
            info = parse_stream_info(url)
            self.platform = info["platform"]
            self.channel = info["channel"]
            self.stream_url = info["stream_url"]
            self.yt_video_id = info["yt_video_id"]
        self.backfill_running = True
        self.backfill_status = "Checking how much live stream time was missed..."
        self._backfill_task = asyncio.create_task(self._run_backfill(join_ts=time.time()))
        return self.status()

    async def _run_backfill(self, join_ts: float | None = None) -> None:
        self.backfill_running = True
        join_ts = join_ts or time.time()
        chan = self.channel or "kreekcraft"
        s_url = self.stream_url or cw_config.KREEKCRAFT_YT_LIVE
        chunk_sec = max(120.0, float(self.backfill_chunk_minutes) * 60.0)
        try:
            self.backfill_status = f"Checking how much stream time we missed on #{chan}..."
            src_info = await asyncio.to_thread(
                backfill.resolve_backfill_source, self.platform, chan, s_url, join_ts
            )
            if not src_info:
                self.backfill_status = "Stream offline — running catch-up on local sample chunk..."
                sample_dir = cw_config.TEMP_DIR / f"_bf_sample_{int(time.time())}"
                try:
                    segs = await asyncio.to_thread(test_pipeline._create_sample_stream_segments, sample_dir)
                    chunk_mp4 = sample_dir / "chunk_01.mp4"
                    await asyncio.to_thread(
                        clipper.extract_clip, segs, chunk_mp4, 0.0, 34.0, False, "blur"
                    )
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    tag = f"{chan}_catchup_{stamp}"
                    self.backfill_status = "Scoring & clipping catch-up chunk 1/1..."
                    out_clip = await asyncio.to_thread(
                        backfill.process_chunk_to_clip,
                        chunk_mp4,
                        tag,
                        self.vertical_mode,
                        self.vertical_layout,
                        self.campaign_mode,
                    )
                    self.backfill_clips_count += 1
                    self.backfill_status = f"Catch-up done (exported {out_clip.name})"
                    if self.auto_post and self.on_clip_exported:
                        try:
                            self.on_clip_exported(out_clip.name)
                        except Exception:
                            pass
                finally:
                    shutil.rmtree(sample_dir, ignore_errors=True)
                return

            # Keep the initial missed_seconds baseline if resuming so chunk numbers 1..N stay consistent
            detected_sec = float(src_info["missed_seconds"])
            missed_sec = self.backfill_missed_seconds if self.backfill_missed_seconds > 30.0 else detected_sec
            self.backfill_missed_seconds = round(missed_sec, 1)
            missed_str = backfill.fmt_duration(missed_sec)

            # Clean up any leftover _bf_* files from temp_recordings
            for old_bf in cw_config.TEMP_DIR.glob("_bf_*"):
                backfill.safe_unlink(old_bf)

            # Build windows covering [0, missed_sec], walking backwards from the join point
            # so the most recent missed gameplay is clipped first.
            windows: list[tuple[float, float]] = []
            cursor = missed_sec
            while cursor > 30.0:
                w_start = max(0.0, cursor - chunk_sec)
                w_len = cursor - w_start
                if w_len >= 30.0:
                    windows.append((w_start, w_len))
                cursor = w_start

            n_chunks = len(windows)
            made = 0
            sem = asyncio.Semaphore(3)  # 3x concurrent catch-up workers

            async def _process_one_window(idx: int, start_s: float, win_len: float) -> bool:
                nonlocal made
                if list(cw_config.OUTPUT_DIR.glob(f"{chan}_catchup{idx:02d}_*.mp4")):
                    made += 1
                    self.backfill_clips_count = max(self.backfill_clips_count, made)
                    return True
                async with sem:
                    # Refresh signed stream URLs if running for > 45 minutes
                    t_range = f"{backfill.fmt_clock(start_s)}–{backfill.fmt_clock(start_s + win_len)}"
                    self.backfill_status = (
                        f"Missed {missed_str} · Scanning 3x parallel [{idx}/{n_chunks}] ({t_range}) · {made} clips exported..."
                    )
                    # For fast audio scanning, probe the central 4 minutes (240s) of a 10m window (~5s on HLS)
                    scan_len = min(win_len, 240.0)
                    scan_start = start_s + max(0.0, (win_len - scan_len) / 2.0)
                    uid = f"{chan}_{idx:02d}_{int(time.time() * 1000) % 100000}"
                    scan_wav = backfill.BACKFILL_TEMP_DIR / f"_bf_scan_{uid}.wav"
                    slice_ts = backfill.BACKFILL_TEMP_DIR / f"_bf_slice_{uid}.ts"
                    try:
                        best_rel, peak_score = await asyncio.to_thread(
                            backfill.scan_window_for_best_30s, src_info, scan_start, scan_len, scan_wav, 30.0
                        )
                        if peak_score < 0.002:
                            return False
                        clip_abs_start = scan_start + best_rel
                        clip_clock = backfill.fmt_clock(clip_abs_start)
                        self.backfill_status = (
                            f"Missed {missed_str} · Chunk {idx}/{n_chunks}: clipping 30s peak at {clip_clock} ({made} ready)..."
                        )
                        ok = await asyncio.to_thread(
                            backfill.download_winning_30s_slice, src_info, clip_abs_start, slice_ts, 31.0
                        )
                        if not ok or not slice_ts.is_file():
                            return False
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        tag = f"{chan}_catchup{idx:02d}_{stamp}"
                        camp = self.active_campaign()
                        is_camp = camp.get("id") != "general" and self.campaign_mode
                        ov_path = campaigns.get_overlay_path_for_campaign(camp.get("id")) if is_camp else None
                        out_clip = await asyncio.to_thread(
                            backfill.process_chunk_to_clip,
                            slice_ts,
                            tag,
                            self.vertical_mode,
                            self.vertical_layout,
                            is_camp,
                            0.0,
                            ov_path,
                            is_camp,
                            camp,
                        )
                        made += 1
                        self.backfill_clips_count = max(self.backfill_clips_count, made)
                        self.backfill_status = (
                            f"Missed {missed_str} · Exported {out_clip.name} ({made}/{n_chunks} chunks done)"
                        )
                        if self.auto_post and self.on_clip_exported:
                            try:
                                self.on_clip_exported(out_clip.name)
                            except Exception:
                                pass
                        return True
                    except asyncio.CancelledError:
                        raise
                    except Exception as chunk_exc:
                        self.backfill_status = f"Chunk {idx}/{n_chunks} skipped ({chunk_exc})"
                        return False
                    finally:
                        backfill.safe_unlink(scan_wav)
                        backfill.safe_unlink(slice_ts)

            # Process in batches of 3 concurrent windows so the most recent gameplay finishes first and 3x faster
            batch_size = 3
            for b_start in range(0, len(windows), batch_size):
                batch = windows[b_start : b_start + batch_size]
                await asyncio.gather(
                    *(
                        _process_one_window(b_start + offset + 1, w_s, w_l)
                        for offset, (w_s, w_l) in enumerate(batch)
                    )
                )

            self.backfill_status = (
                f"Catch-up finished ({made} clip{'s' if made != 1 else ''} from {missed_str} missed stream time)"
            )
        except asyncio.CancelledError:
            self.backfill_status = "Stopped"
            raise
        except Exception as exc:
            self.backfill_status = f"Catch-up error: {exc}"
        finally:
            self.backfill_running = False

    def list_clips(self) -> list[dict]:
        clips = []
        active_camp = self.active_campaign()
        files = [p for p in cw_config.OUTPUT_DIR.glob("*.mp4") if not p.name.startswith(".tmp_")]
        for p in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
            st = p.stat()
            srt = p.with_suffix(".srt")
            meta = self.read_meta(p)
            cid = meta.get("campaign_id") or active_camp.get("id")
            camp_rules = campaigns.get_campaign(cid)
            is_camp = camp_rules.get("id") != "general"
            title = meta.get("title") or p.stem
            desc = meta.get("description") or ""
            tags = meta.get("hashtags") or list(camp_rules.get("required_hashtags") or cw_config.CAMPAIGN_HASHTAGS)
            add_link = False if (is_camp and camp_rules.get("no_links", True)) else bool(meta.get("add_source_link", True))
            ov_applied = bool(meta.get("overlay_applied") or (is_camp and cw_config.get_overlay_path() is not None))
            cliff_txt = meta.get("cliffhanger_text") or captioner.derive_cliffhanger_text(
                meta.get("transcript") or "", camp_rules.get("cliffhanger_template"), camp_rules.get("streamer")
            )
            clips.append({
                "name": p.name,
                "video": f"/watcher-media/{p.name}",
                "srt": f"/watcher-media/{srt.name}" if srt.exists() else None,
                "size_mb": round(st.st_size / (1024 * 1024), 2),
                "created": st.st_mtime,
                "title": title,
                "description": desc,
                "hashtags": tags,
                "cliffhanger_text": cliff_txt,
                "campaign_id": camp_rules.get("id"),
                "campaign_name": camp_rules.get("name"),
                "social_caption": meta.get("social_caption") or f"{desc} {' '.join('#' + str(t).lstrip('#') for t in tags)}".strip(),
                "transcript": meta.get("transcript") or "",
                "channel": meta.get("channel") or self.channel,
                "privacy": meta.get("privacy") or self.auto_privacy,
                "account_id": meta.get("account_id") or self.auto_account_id,
                "add_source_link": add_link,
                "overlay_applied": ov_applied,
                "compliance": evaluate_campaign_compliance(title, desc, tags, add_link, ov_applied, camp_rules),
                "youtube": meta.get("youtube"),
                "tiktok": meta.get("tiktok"),
                "instagram": meta.get("instagram"),
                "tiktok_options": meta.get("tiktok_options") or {},
                "instagram_options": meta.get("instagram_options") or {},
                "target_platforms": meta.get("target_platforms") or ["youtube"],
                "approval_status": meta.get("approval_status") or "pending",
                "made_for_kids": bool(meta.get("made_for_kids", False)),
                "notify_subscribers": bool(meta.get("notify_subscribers", True)),
            })
        return clips

    def delete_clip(self, name: str) -> None:
        target = self.resolve_clip_path(name)
        meta = self.read_meta(target)
        for p in ("youtube", "tiktok", "instagram"):
            if (meta.get(p) or {}).get("status") in ("queued", "uploading"):
                raise RuntimeError(f"This clip is being posted to {p}. Wait for it to finish.")
        target.unlink()
        target.with_suffix(".srt").unlink(missing_ok=True)
        target.with_suffix(".json").unlink(missing_ok=True)

    def clear_clips(self, keep_posted: bool = True, channel: str | None = None) -> dict:
        """Bulk delete live stream clips (optionally keeping clips that have already been posted)."""
        deleted = 0
        kept = 0
        files = [p for p in cw_config.OUTPUT_DIR.glob("*.mp4") if not p.name.startswith(".tmp_")]
        for p in files:
            meta = self.read_meta(p)
            if channel and (meta.get("channel") or "").lower() != channel.lower():
                kept += 1
                continue
            busy = any((meta.get(plat) or {}).get("status") in ("queued", "uploading") for plat in ("youtube", "tiktok", "instagram"))
            if busy:
                kept += 1
                continue
            posted = any((meta.get(plat) or {}).get("status") == "done" for plat in ("youtube", "tiktok", "instagram"))
            if keep_posted and posted:
                kept += 1
                continue
            p.unlink(missing_ok=True)
            p.with_suffix(".srt").unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)
            deleted += 1
        self.backfill_clips_count = len(list(cw_config.OUTPUT_DIR.glob("*_catchup*.mp4")))
        return {"ok": True, "deleted": deleted, "kept": kept}

    def status(self) -> dict:
        rec = self.recorder
        chat = self.chat
        det = chat.detector if chat else None
        baseline = round(det.baseline, 1) if det else 0.0
        threshold = max(self.min_messages, round(baseline * self.multiplier, 1))
        last = det.spikes[-1] if det and det.spikes else None
        uptime = int(time.time() - rec.started_at) if (rec and rec.started_at) else 0
        return {
            "running": self.running,
            "platform": self.platform,
            "channel": self.channel,
            "stream_url": self.stream_url,
            "campaign_mode": self.campaign_mode,
            "campaign": self.active_campaign(),
            "overlay": self.overlay_status(),
            "is_live": bool(rec and rec.is_live),
            "uptime_seconds": max(0, uptime),
            "segments": rec.active_segment_count() if rec else 0,
            "irc_connected": bool(chat and chat.connected),
            "current_count": chat.current_count if chat else 0,
            "last_window_count": chat.last_window_count if chat else 0,
            "baseline": baseline,
            "threshold": threshold,
            "multiplier": self.multiplier,
            "min_messages": self.min_messages,
            "vertical_mode": self.vertical_mode,
            "vertical_layout": self.vertical_layout,
            "auto_post": self.auto_post,
            "auto_privacy": self.auto_privacy,
            "auto_account_id": self.auto_account_id,
            "backfill": {
                "enabled": self.backfill_enabled,
                "missed_seconds": self.backfill_missed_seconds,
                "running": self.backfill_running,
                "status": self.backfill_status,
                "clips_count": self.backfill_clips_count,
            },
            "spikes_count": len(det.spikes) if det else 0,
            "last_spike": (
                {
                    "count": last.count,
                    "baseline": last.baseline,
                    "multiplier": last.multiplier,
                    "timestamp": last.timestamp,
                }
                if last
                else None
            ),
            "busy": self.busy,
            "status_note": self.status_note,
            "clips": self.list_clips(),
        }


controller = WatcherController()
