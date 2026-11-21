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


def build_default_metadata(
    srt_path: Path, channel: str | None = None, campaign_mode: bool | None = None
) -> dict:
    """Build ready-to-post Shorts/TikTok/Reels metadata following KreekCraft GTA 6 campaign rules."""
    if campaign_mode is None:
        campaign_mode = config.CAMPAIGN_MODE

    chan = (channel or "").strip().lstrip("#@").lower()
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

    if campaign_mode:
        if hook_clean:
            title = f"{hook_clean} | KreekCraft GTA 6"
        else:
            title = "KreekCraft GTA 6 Stream Highlight"
        title = strip_urls(title).replace("<", "").replace(">", "")[:100].strip()

        # Strict campaign rule: include "Watch KreekCraft on YouTube and Twitch", required hashtags, NO links/URLs.
        if hook_clean:
            description = f"{hook_clean}\n\n{config.CAMPAIGN_CTA}"
        else:
            description = config.CAMPAIGN_CTA
        hashtags = list(config.CAMPAIGN_HASHTAGS)
        social_caption = f"{config.CAMPAIGN_CTA} #GTA6 #KreekCraft #GrandTheftAutoVI"
        return {
            "title": title,
            "description": strip_urls(description),
            "hashtags": hashtags,
            "social_caption": social_caption,
            "transcript": transcript,
            "channel": chan or "kreekcraft",
            "campaign_mode": True,
            "add_source_link": False,
            "overlay_applied": config.get_overlay_path() is not None,
        }

    # Non-campaign fallback
    if hook_clean:
        title = f"{hook_clean} | #{chan}" if chan else hook_clean
    else:
        title = f"{chan} Live Hype Moment #shorts" if chan else "Live Stream Hype Clip #shorts"
    title = title.replace("<", "").replace(">", "")[:100].strip()

    desc_parts = []
    if transcript:
        readable = transcript[:1].upper() + transcript[1:].lower() if transcript.isupper() else transcript
        desc_parts.append(f'"{readable[:350]}"')
    if chan:
        desc_parts.append(f"Clipped live from https://www.twitch.tv/{chan}")
    description = "\n\n".join(desc_parts) if desc_parts else "Live stream highlight clip."

    hashtags = ["shorts"]
    if chan:
        hashtags.append(chan)
    for tag in ("twitch", "gaming", "livestream", "clips"):
        if tag not in hashtags:
            hashtags.append(tag)

    return {
        "title": title,
        "description": description,
        "hashtags": hashtags[:5],
        "transcript": transcript,
        "channel": chan or None,
        "campaign_mode": False,
        "add_source_link": True,
        "overlay_applied": config.get_overlay_path() is not None,
    }


def burn_captions(video_path: Path, srt_path: Path, out_path: Path) -> Path:
    """Burn bold short-form SRT captions into video_path using ffmpeg."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    local_srt = out_path.parent / f".tmp_{srt_path.name}"
    copied_srt = False
    if srt_path.resolve().parent != out_path.resolve().parent:
        shutil.copy2(srt_path, local_srt)
        copied_srt = True
        srt_arg = local_srt.name
    else:
        srt_arg = srt_path.name

    tmp_mp4 = out_path.with_name(f".tmp_{out_path.name}")
    style = (
        "FontName=Arial Black,FontSize=18,PrimaryColour=&H0000FFFF,"
        "OutlineColour=&H00000000,BackColour=&H96000000,Bold=1,"
        "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=48"
    )
    try:
        has_cues = srt_path.exists() and bool(srt_path.read_text(encoding="utf-8").strip())
        if has_cues:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path.resolve()),
                "-vf", f"subtitles={srt_arg}:force_style='{style}'",
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
        if copied_srt:
            local_srt.unlink(missing_ok=True)
        tmp_mp4.unlink(missing_ok=True)


def caption_clip(
    video_path: Path,
    out_path: Path,
    model_name: str = config.WHISPER_MODEL,
    srt_path: Path | None = None,
    channel: str | None = None,
    campaign_mode: bool | None = None,
) -> tuple[Path, Path]:
    """Generate .srt captions via Whisper, write Shorts metadata sidecar, and burn captions onto MP4."""
    srt_target = srt_path or out_path.with_suffix(".srt")
    transcribe_to_srt(video_path, srt_target, model_name=model_name)
    burn_captions(video_path, srt_target, out_path)
    meta_path = out_path.with_suffix(".json")
    if not meta_path.exists():
        meta = build_default_metadata(srt_target, channel=channel, campaign_mode=campaign_mode)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path, srt_target
