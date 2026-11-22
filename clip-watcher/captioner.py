"""Local Whisper transcription to SRT, KreekCraft GTA 6 campaign metadata, and caption burn-in."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

import config

_URL_RE = re.compile(r"https?://\S+|www\.\S+|\b[\w-]+\.(?:com|tv|gg|be|net|org)(?:/\S*)?", re.I)


def strip_urls(text: str) -> str:
    """Remove any URLs or domain links to comply with campaign caption rules."""
    cleaned = _URL_RE.sub("", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _read_wav_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    hrs, rem = divmod(ms, 3_600_000)
    mins, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def _group_words(
    words: list[tuple[str, float, float]], max_words: int = 4, max_chars: int = 22
) -> list[tuple[float, float, str]]:
    """Group word timestamps into punchy short-form subtitle cues."""
    cues: list[tuple[float, float, str]] = []
    cur: list[tuple[str, float, float]] = []
    for word, start, end in words:
        chars = sum(len(w[0]) for w in cur) + len(cur)
        gap = (start - cur[-1][2]) if cur else 0.0
        if cur and (len(cur) >= max_words or chars + len(word) > max_chars or gap > 0.6):
            cues.append((cur[0][1], cur[-1][2], " ".join(w[0] for w in cur)))
            cur = []
        cur.append((word, start, end))
        if word[-1:] in ".?!,":
            cues.append((cur[0][1], cur[-1][2], " ".join(w[0] for w in cur)))
            cur = []
    if cur:
        cues.append((cur[0][1], cur[-1][2], " ".join(w[0] for w in cur)))
    return cues


def _transcribe_words(wav_path: Path, model_name: str) -> list[tuple[str, float, float]]:
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            model_name, device="cpu", compute_type="int8", download_root=str(config.MODELS_DIR)
        )
        segments, _ = model.transcribe(_read_wav_16k(wav_path), word_timestamps=True, vad_filter=True)
        words: list[tuple[str, float, float]] = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    tok = w.word.strip()
                    if tok:
                        words.append((tok, float(w.start), float(w.end)))
            elif seg.text.strip():
                words.append((seg.text.strip(), float(seg.start), float(seg.end)))
        return words
    except ImportError:
        import whisper

        model = whisper.load_model(model_name)
        result = model.transcribe(str(wav_path), word_timestamps=True)
        words = []
        for seg in result.get("segments", []):
            for w in seg.get("words") or []:
                tok = w["word"].strip()
                if tok:
                    words.append((tok, float(w["start"]), float(w["end"])))
            if not seg.get("words") and seg.get("text", "").strip():
                words.append((seg["text"].strip(), float(seg["start"]), float(seg["end"])))
        return words


def transcribe_to_srt(
    video_path: Path, srt_path: Path, model_name: str = config.WHISPER_MODEL
) -> Path:
    """Transcribe video audio locally with Whisper and write an SRT subtitle file."""
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path = srt_path.with_suffix(".tmp.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(wav_path)],
            check=True, capture_output=True, text=True,
        )
        words = _transcribe_words(wav_path, model_name)
    finally:
        wav_path.unlink(missing_ok=True)

    cues = _group_words(words)
    blocks = [
        f"{idx}\n{_srt_time(s)} --> {_srt_time(max(e, s + 0.2))}\n{text.upper()}\n"
        for idx, (s, e, text) in enumerate(cues, start=1)
    ]
    srt_path.write_text("\n".join(blocks), encoding="utf-8")
    return srt_path


def extract_srt_transcript(srt_path: Path) -> str:
    """Extract plain spoken text from an SRT file."""
    if not srt_path.is_file():
        return ""
    lines = []
    for raw in srt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        lines.append(line)
    return " ".join(lines).strip()


def _ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc_ass(text: str) -> str:
    return (text or "").replace("\\", "").replace("{", "(").replace("}", ")")


def derive_cliffhanger_text(transcript: str, custom_template: str | None = None, streamer: str | None = None) -> str:
    """Create a short, clean white-box hook banner (no emojis that render as broken boxes in libass)."""
    def _clean_hook(s: str) -> str:
        # Strip emojis/non-ASCII symbols that render as tofu boxes in Arial Black / libass
        s = re.sub(r"[^\x20-\x7E]", "", s or "")
        return " ".join(s.split()).strip(" \"'-—:")

    tmpl = _clean_hook(custom_template or "")
    sentences = [s.strip() for s in re.split(r"(?<=[.?!])\s+", strip_urls(transcript or "")) if s.strip()]
    first = _clean_hook(sentences[0] if sentences else (transcript or "").strip())
    if len(first) > 38:
        first = first[:35].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."
    if tmpl and "{hook}" in tmpl.lower():
        return re.sub(r"\{hook\}", first or "WAIT FOR IT...", tmpl, flags=re.I)[:44].upper()
    if tmpl:
        return tmpl[:42].upper()
    if first and len(first.split()) >= 2:
        return first[:42].upper()
    tag = _clean_hook(streamer or "THIS STREAM").upper()
    return f"WAIT FOR IT ({tag})"[:42]


def build_default_metadata(
    srt_path: Path,
    channel: str | None = None,
    campaign_mode: bool | None = None,
    campaign_rules: dict | None = None,
    cliffhanger_text: str | None = None,
) -> dict:
    """Build ready-to-post Shorts/TikTok/Reels metadata following active campaign rules or general mode."""
    rules = campaign_rules or {}
    if campaign_mode is None:
        campaign_mode = bool(rules.get("id", "kreekcraft-gta6") != "general") if rules else config.CAMPAIGN_MODE

    chan = (channel or rules.get("streamer") or "").strip().lstrip("#@").lower()
    if not chan:
        stem = srt_path.stem
        prefix = stem.split("_")[0].lower()
        if prefix and prefix not in ("test", "clip", "raw", "demo"):
            chan = prefix

    transcript = strip_urls(extract_srt_transcript(srt_path))
    sentences = [s.strip() for s in re.split(r"(?<=[.?!])\s+", transcript) if s.strip()]
    hook = sentences[0] if sentences else transcript[:70].strip()
    hook_clean = (hook[:1].upper() + hook[1:].lower()) if (hook and hook.isupper()) else hook
    if len(hook_clean) > 65:
        hook_clean = hook_clean[:62].rstrip(" ,;:") + "..."

    cliff_banner = cliffhanger_text or derive_cliffhanger_text(
        transcript, rules.get("cliffhanger_template"), rules.get("streamer") or chan
    )

    if campaign_mode:
        camp_id = rules.get("id") or "kreekcraft-gta6"
        camp_name = rules.get("name") or "KreekCraft GTA 6"
        streamer_label = rules.get("streamer") or "KreekCraft"
        cta = (rules.get("required_cta") if "required_cta" in rules else config.CAMPAIGN_CTA) or ""
        tags = list(rules.get("required_hashtags") or config.CAMPAIGN_HASHTAGS)
        no_links = bool(rules.get("no_links", True))

        suffix = camp_name.replace(" Clipping", "")
        if hook_clean:
            title = f"{hook_clean} | {suffix}"
        else:
            title = f"{streamer_label} Stream Highlight | {suffix}"
        if no_links:
            title = strip_urls(title)
        title = title.replace("<", "").replace(">", "")[:100].strip()

        if hook_clean and cta:
            description = f"{hook_clean}\n\n{cta}"
        elif cta:
            description = cta
        else:
            description = hook_clean or f"{streamer_label} stream highlight"
        if no_links:
            description = strip_urls(description)

        tags_str = " ".join(f"#{str(t).lstrip('#')}" for t in tags)
        social_caption = f"{description} {tags_str}".strip()
        return {
            "title": title,
            "description": description,
            "hashtags": tags,
            "social_caption": social_caption,
            "cliffhanger_text": cliff_banner,
            "transcript": transcript,
            "channel": chan or "kreekcraft",
            "campaign_id": camp_id,
            "campaign_mode": True,
            "add_source_link": not no_links,
            "overlay_applied": config.get_overlay_path() is not None,
        }

    # General mode (non-campaign editing)
    if hook_clean:
        title = f"{hook_clean} | #{chan}" if chan else hook_clean
    else:
        title = f"{chan} Live Highlight #shorts" if chan else "Live Stream Highlight Clip #shorts"
    title = title.replace("<", "").replace(">", "")[:100].strip()

    desc_parts = []
    if transcript:
        readable = transcript[:1].upper() + transcript[1:].lower() if transcript.isupper() else transcript
        desc_parts.append(f'"{readable[:350]}"')
    if chan:
        desc_parts.append(f"Clipped from #{chan}")
    description = "\n\n".join(desc_parts) if desc_parts else "Highlight clip."

    hashtags = list(rules.get("required_hashtags") or ["shorts", "clips", "viral"])
    if chan and chan not in hashtags:
        hashtags.insert(1, chan)

    return {
        "title": title,
        "description": description,
        "hashtags": hashtags[:5],
        "cliffhanger_text": cliff_banner,
        "transcript": transcript,
        "channel": chan or None,
        "campaign_id": "general",
        "campaign_mode": False,
        "add_source_link": True,
        "overlay_applied": False,
    }


def _build_cliffhanger_and_captions_ass(
    srt_path: Path,
    cliffhanger_text: str | None = None,
    cliffhanger_duration: float = 4.0,
    caption_style: str = "bw",
) -> str:
    """Build an ASS script containing BOTH the white-background black-text cliffhanger hook banner
    at the top of the video AND the bottom word-grouped subtitles (white B&W by default)."""
    # ASS colour format is &HAABBGGRR (White = &H00FFFFFF, Yellow = &H0000FFFF)
    cap_color = "&H0000FFFF" if caption_style == "yellow" else "&H00FFFFFF"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,68,{cap_color},{cap_color},&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,5,2,2,70,70,340,1
Style: CliffBox,Arial Black,44,&H000A0A0A,&H000A0A0A,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,14,0,8,90,90,210,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    if cliffhanger_text:
        clean_hook = _esc_ass(cliffhanger_text.strip().upper())
        # Render immediately at frame 0 (no fade-in delay) so even t=0.0s shows the solid white box
        events.append(
            f"Dialogue: 2,{_ass_time(0.0)},{_ass_time(max(4.0, cliffhanger_duration))},CliffBox,,0,0,0,,"
            f"{clean_hook}"
        )

    if srt_path.is_file():
        raw = srt_path.read_text(encoding="utf-8", errors="ignore")
        for block in re.split(r"\r?\n\r?\n", raw.strip()):
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            t_line = lines[1] if "-->" in lines[1] else (lines[0] if "-->" in lines[0] else "")
            if "-->" not in t_line:
                continue
            text_lines = lines[2:] if t_line == lines[1] else lines[1:]
            cue_text = _esc_ass(" ".join(text_lines))
            if not cue_text:
                continue
            m = re.match(
                r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", t_line
            )
            if not m:
                continue
            s_sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0
            e_sec = int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7)) + int(m.group(8)) / 1000.0
            events.append(f"Dialogue: 1,{_ass_time(s_sec)},{_ass_time(e_sec)},Cap,,0,0,0,,{cue_text}")

    return head + "\n".join(events) + "\n"


def burn_captions(
    video_path: Path,
    srt_path: Path,
    out_path: Path,
    cliffhanger_text: str | None = None,
    cliffhanger_duration: float = 30.0,
    caption_style: str = "bw",
) -> Path:
    """Burn white-box cliffhanger hook text + bold short-form captions into video_path using ffmpeg."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path = out_path.parent / f".tmp_{out_path.stem}.ass"
    tmp_mp4 = out_path.with_name(f".tmp_{out_path.name}")
    try:
        has_cues = (srt_path.exists() and bool(srt_path.read_text(encoding="utf-8").strip())) or bool(cliffhanger_text)
        if has_cues:
            ass_content = _build_cliffhanger_and_captions_ass(
                srt_path,
                cliffhanger_text=cliffhanger_text,
                cliffhanger_duration=cliffhanger_duration,
                caption_style=caption_style,
            )
            ass_path.write_text(ass_content, encoding="utf-8")
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path.resolve()),
                "-vf", f"ass={ass_path.name}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-movflags", "+faststart", tmp_mp4.name,
            ]
            proc = subprocess.run(cmd, cwd=out_path.parent, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg caption burn-in failed: {proc.stderr.strip()[-400:]}")
            tmp_mp4.replace(out_path)
        elif video_path.resolve() != out_path.resolve():
            shutil.copy2(video_path, out_path)
        return out_path
    finally:
        ass_path.unlink(missing_ok=True)
        tmp_mp4.unlink(missing_ok=True)


def caption_clip(
    video_path: Path,
    out_path: Path,
    model_name: str = config.WHISPER_MODEL,
    srt_path: Path | None = None,
    channel: str | None = None,
    campaign_mode: bool | None = None,
    campaign_rules: dict | None = None,
    cliffhanger_enabled: bool = True,
    cliffhanger_text: str | None = None,
) -> tuple[Path, Path]:
    """Generate .srt captions via Whisper, write Shorts metadata sidecar, and burn cliffhanger + captions onto MP4."""
    srt_target = srt_path or out_path.with_suffix(".srt")
    transcribe_to_srt(video_path, srt_target, model_name=model_name)
    meta = build_default_metadata(
        srt_target,
        channel=channel,
        campaign_mode=campaign_mode,
        campaign_rules=campaign_rules,
        cliffhanger_text=cliffhanger_text,
    )
    rules = campaign_rules or {}
    use_cliff = bool(rules.get("cliffhanger_enabled", cliffhanger_enabled))
    banner_txt = meta.get("cliffhanger_text") if use_cliff else None
    banner_dur = float(rules.get("cliffhanger_duration", 30.0))
    cap_style = (rules.get("caption_style") or "bw").strip().lower()
    burn_captions(
        video_path,
        srt_target,
        out_path,
        cliffhanger_text=banner_txt,
        cliffhanger_duration=banner_dur,
        caption_style=cap_style,
    )
    meta_path = out_path.with_suffix(".json")
    if not meta_path.exists():
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path, srt_target
