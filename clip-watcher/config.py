"""Shared configuration and KreekCraft GTA 6 campaign rules for clip-watcher."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

if shutil.which("ffmpeg") is None:
    winget_links = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"
    if winget_links.is_dir():
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(winget_links)

TEMP_DIR = BASE_DIR / "temp_recordings"
OUTPUT_DIR = BASE_DIR / "output_clips"
ASSETS_DIR = BASE_DIR / "assets"
MODELS_DIR = BASE_DIR.parent / "data" / "models" / "whisper"

for _d in (TEMP_DIR, OUTPUT_DIR, ASSETS_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "kreekcraft").strip().lstrip("#").lower()
STREAM_URL = os.environ.get("STREAM_URL", "https://www.youtube.com/live/dy-oCN2NxfA").strip()
SPIKE_MULTIPLIER = float(os.environ.get("SPIKE_MULTIPLIER", "2.5"))
MIN_SPIKE_MESSAGES = int(os.environ.get("MIN_SPIKE_MESSAGES", "10"))
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "60"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny").strip()
VERTICAL_MODE = _bool_env("VERTICAL_MODE", True)
VERTICAL_LAYOUT = os.environ.get("VERTICAL_LAYOUT", "blur").strip().lower()
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "5"))
RETENTION_SECONDS = int(os.environ.get("RETENTION_SECONDS", "300"))
POST_SPIKE_WAIT_SECONDS = 18.0

# KreekCraft GTA 6 Campaign defaults
CAMPAIGN_MODE = _bool_env("CAMPAIGN_MODE", True)
CAMPAIGN_CTA = "Watch KreekCraft on YouTube and Twitch! 🔴"
CAMPAIGN_HASHTAGS = ["GTA6", "KreekCraft", "GrandTheftAutoVI"]
KREEKCRAFT_YT_LIVE = "https://www.youtube.com/live/dy-oCN2NxfA"
KREEKCRAFT_TWITCH_LIVE = "https://www.twitch.tv/kreekcraft"


def get_overlay_path() -> Path | None:
    """Return the active KreekCraft logo overlay file in assets/, if present."""
    env_path = os.environ.get("LOGO_OVERLAY_PATH", "").strip()
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    for ext in (".png", ".webp", ".gif", ".mov", ".mp4"):
        p = ASSETS_DIR / f"logo_overlay{ext}"
        if p.is_file():
            return p
    found = sorted(ASSETS_DIR.glob("logo_overlay.*"))
    return found[0] if found else None
