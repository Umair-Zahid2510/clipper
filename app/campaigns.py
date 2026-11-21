"""Multi-campaign profile manager & rules brief parser for clipper.io.

Allows users to switch between:
- 'general': Standard clipper.io mode (no mandatory campaign logo overlay, normal captions + #shorts, cliffhanger hook banner optional)
- Any campaign profile (e.g. 'kreekcraft-gta6' or newly created campaigns):
  - Custom campaign logo overlay file (stored in clip-watcher/assets/campaigns/<id>_overlay.<ext>)
  - Required caption CTA line (e.g. 'Watch KreekCraft on YouTube and Twitch')
  - Required hashtags (e.g. ['GTA6', 'KreekCraft', 'GrandTheftAutoVI'])
  - No-links / strip-URLs rule
  - Cliffhanger white-background black-text hook banner settings (enabled, custom prefix/template, duration)
  - CPM rate ($ per 1K views), Max payout per clip, and Minimum engagement rate %
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from . import config as app_config

CAMPAIGNS_PATH = app_config.DATA_DIR / "campaigns.json"
CAMPAIGN_ASSETS_DIR = app_config.ROOT / "clip-watcher" / "assets"
_lock = threading.Lock()

DEFAULT_KREEKCRAFT_CAMPAIGN = {
    "id": "kreekcraft-gta6",
    "name": "KreekCraft GTA 6 Clipping",
    "streamer": "KreekCraft",
    "stream_urls": [
        "https://www.youtube.com/live/dy-oCN2NxfA",
        "https://www.youtube.com/@Kreek/streams",
        "https://www.twitch.tv/kreekcraft",
    ],
    "required_cta": "Watch KreekCraft on YouTube and Twitch! 🔴",
    "required_hashtags": ["GTA6", "KreekCraft", "GrandTheftAutoVI"],
    "no_links": True,
    "require_overlay": True,
    "overlay_filename": "logo_overlay.webp",
    "cliffhanger_enabled": True,
    "cliffhanger_template": "WAIT FOR IT... 😱",
    "cliffhanger_duration": 4.0,
    "cpm_usd": 1.0,
    "max_payout_usd": 300.0,
    "min_engagement_pct": 0.8,
    "caption_style": "bw",
    "notes": "Official logo overlay must remain visible start-to-finish. Include 'Watch KreekCraft on YouTube and Twitch' and #GTA6 #KreekCraft #GrandTheftAutoVI with no URLs.",
}

GENERAL_PROFILE = {
    "id": "general",
    "name": "General Mode (No Campaign Rules)",
    "streamer": "",
    "stream_urls": [],
    "required_cta": "",
    "required_hashtags": ["shorts", "clips", "viral"],
    "no_links": False,
    "require_overlay": False,
    "overlay_filename": None,
    "cliffhanger_enabled": True,
    "cliffhanger_template": "",
    "cliffhanger_duration": 3.5,
    "cpm_usd": 0.0,
    "max_payout_usd": 0.0,
    "min_engagement_pct": 0.0,
    "caption_style": "yellow",
    "notes": "Standard clipper.io editing: no campaign logo overlay, normal descriptions with source links allowed, and automatic cliffhanger hook banner from transcript.",
}


def _default_store() -> dict:
    # Check if logo_overlay.* exists in clip-watcher/assets/
    ov_name = None
    if CAMPAIGN_ASSETS_DIR.is_dir():
        for ext in (".webp", ".png", ".gif", ".mov", ".mp4"):
            p = CAMPAIGN_ASSETS_DIR / f"logo_overlay{ext}"
            if p.is_file():
                ov_name = p.name
                break
    kc = dict(DEFAULT_KREEKCRAFT_CAMPAIGN)
    kc["overlay_filename"] = ov_name or kc["overlay_filename"]
    return {
        "active_id": "kreekcraft-gta6",
        "campaigns": {
            "general": dict(GENERAL_PROFILE),
            "kreekcraft-gta6": kc,
        },
    }


def _read_store() -> dict:
    with _lock:
        try:
            data = json.loads(CAMPAIGNS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("campaigns"), dict):
                data["campaigns"].setdefault("general", dict(GENERAL_PROFILE))
                data["campaigns"].setdefault("kreekcraft-gta6", dict(DEFAULT_KREEKCRAFT_CAMPAIGN))
                # Repair any campaign profile where streamer was parsed as the verb "Put"
                for cid, c in data["campaigns"].items():
                    if c.get("streamer") == "Put" and "clipup" in cid.lower():
                        c["name"] = "ClipUp GTA 6 Campaign"
                        c["streamer"] = "GTA6"
                        c["required_cta"] = ""
                        c["cpm_usd"] = 0.30
                if data.get("active_id") not in data["campaigns"]:
                    data["active_id"] = "kreekcraft-gta6"
                return data
        except (OSError, ValueError):
            pass
        return _default_store()


def _write_store(store: dict) -> None:
    with _lock:
        CAMPAIGNS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CAMPAIGNS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CAMPAIGNS_PATH)


def list_campaigns() -> dict:
    store = _read_store()
    active_id = store["active_id"]
    items = []
    for cid, c in store["campaigns"].items():
        ov_file = c.get("overlay_filename")
        ov_path = (CAMPAIGN_ASSETS_DIR / ov_file) if ov_file else None
        has_ov = bool(ov_path and ov_path.is_file())
        items.append({
            **c,
            "active": cid == active_id,
            "is_campaign": cid != "general",
            "overlay_present": has_ov,
            "overlay_url": f"/watcher-assets/{ov_file}" if has_ov else None,
        })
    active = next((x for x in items if x["id"] == active_id), items[0])
    return {
        "active_id": active_id,
        "active": active,
        "campaigns": items,
    }


def get_campaign(campaign_id: str | None = None) -> dict:
    state = list_campaigns()
    if not campaign_id:
        return state["active"]
    found = next((x for x in state["campaigns"] if x["id"] == campaign_id), None)
    return found or state["active"]


def get_overlay_path_for_campaign(campaign_id: str | None = None) -> Path | None:
    c = get_campaign(campaign_id)
    if c["id"] == "general" or not c.get("require_overlay"):
        return None
    ov_file = c.get("overlay_filename")
    if ov_file:
        p = CAMPAIGN_ASSETS_DIR / ov_file
        if p.is_file():
            return p
    return None


def select_campaign(campaign_id: str) -> dict:
    store = _read_store()
    if campaign_id not in store["campaigns"]:
        raise ValueError(f"Campaign '{campaign_id}' not found.")
    store["active_id"] = campaign_id
    _write_store(store)
    return list_campaigns()


def save_campaign(payload: dict) -> dict:
    store = _read_store()
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("Campaign name is required.")
    cid = (payload.get("id") or "").strip().lower()
    if not cid:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32]
        cid = slug or f"camp-{uuid.uuid4().hex[:6]}"
    existing = store["campaigns"].get(cid, {})

    raw_tags = payload.get("required_hashtags")
    if isinstance(raw_tags, str):
        tags = [t.lstrip("#").strip() for t in re.split(r"[\s,]+", raw_tags) if t.lstrip("#").strip()]
    elif isinstance(raw_tags, list):
        tags = [str(t).lstrip("#").strip() for t in raw_tags if str(t).lstrip("#").strip()]
    else:
        tags = existing.get("required_hashtags") or ["shorts"]

    raw_urls = payload.get("stream_urls")
    if isinstance(raw_urls, str):
        urls = [u.strip() for u in re.split(r"[\r\n,]+", raw_urls) if u.strip()]
    elif isinstance(raw_urls, list):
        urls = [str(u).strip() for u in raw_urls if str(u).strip()]
    else:
        urls = existing.get("stream_urls") or []

    entry = {
        "id": cid,
        "name": name,
        "streamer": (payload.get("streamer") or existing.get("streamer") or "").strip(),
        "stream_urls": urls,
        "required_cta": (payload.get("required_cta") if "required_cta" in payload else existing.get("required_cta") or "").strip(),
        "required_hashtags": tags,
        "no_links": bool(payload.get("no_links", existing.get("no_links", cid != "general"))),
        "require_overlay": bool(payload.get("require_overlay", existing.get("require_overlay", cid != "general"))),
        "overlay_filename": payload.get("overlay_filename") or existing.get("overlay_filename"),
        "cliffhanger_enabled": bool(payload.get("cliffhanger_enabled", existing.get("cliffhanger_enabled", True))),
        "cliffhanger_template": (payload.get("cliffhanger_template") if "cliffhanger_template" in payload else existing.get("cliffhanger_template") or "").strip(),
        "cliffhanger_duration": float(payload.get("cliffhanger_duration", existing.get("cliffhanger_duration", 3.5))),
        "cpm_usd": float(payload.get("cpm_usd", existing.get("cpm_usd", 1.0 if cid != "general" else 0.0))),
        "max_payout_usd": float(payload.get("max_payout_usd", existing.get("max_payout_usd", 300.0 if cid != "general" else 0.0))),
        "min_engagement_pct": float(payload.get("min_engagement_pct", existing.get("min_engagement_pct", 0.8 if cid != "general" else 0.0))),
        "caption_style": (payload.get("caption_style") or existing.get("caption_style") or "bw").strip(),
        "notes": (payload.get("notes") if "notes" in payload else existing.get("notes") or "").strip(),
        "updated_at": time.time(),
    }
    store["campaigns"][cid] = entry
    if payload.get("set_active", True):
        store["active_id"] = cid
    _write_store(store)
    return list_campaigns()


def save_campaign_overlay(campaign_id: str, filename: str, content: bytes) -> dict:
    ext = Path(filename or "").suffix.lower()
    if ext not in (".png", ".webp", ".gif", ".mov", ".mp4"):
        raise ValueError("Upload a PNG, WebP, GIF, MOV, or MP4 logo overlay file.")
    if not content:
        raise ValueError("Uploaded overlay file is empty.")
    store = _read_store()
    if campaign_id not in store["campaigns"]:
        raise ValueError("Campaign not found.")
    CAMPAIGN_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-z0-9_-]", "", campaign_id.lower()) or "campaign"
    dest_name = f"logo_overlay{ext}" if safe_id == "kreekcraft-gta6" else f"overlay_{safe_id}{ext}"
    dest = CAMPAIGN_ASSETS_DIR / dest_name
    dest.write_bytes(content)
    store["campaigns"][campaign_id]["overlay_filename"] = dest_name
    store["campaigns"][campaign_id]["require_overlay"] = True
    _write_store(store)
    return list_campaigns()


def delete_campaign(campaign_id: str) -> dict:
    if campaign_id in ("general", "kreekcraft-gta6"):
        raise ValueError("Built-in profiles ('general' and 'kreekcraft-gta6') cannot be deleted, only edited.")
    store = _read_store()
    if campaign_id in store["campaigns"]:
        del store["campaigns"][campaign_id]
    if store["active_id"] == campaign_id:
        store["active_id"] = "general"
    _write_store(store)
    return list_campaigns()


def parse_rules_brief(text: str) -> dict:
    """Extract campaign rules automatically when a user pastes a raw ClipLaunch / Notion / Discord campaign brief."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Paste a campaign rules brief to parse.")

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    name = lines[0][:80] if lines else "New Streamer Campaign"
    name = re.sub(r":\s*Full Rules Brief.*$", "", name, flags=re.I).strip()

    # Extract hashtags (#Tag)
    tags = []
    for m in re.finditer(r"#([A-Za-z0-9_]{2,35})", raw):
        t = m.group(1)
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)

    # Extract CTA in quotes after "Include"
    cta = ""
    cta_m = re.search(r'[Ii]nclude\s+[“"\']([^”"\']{5,120})[”"\']', raw)
    if cta_m:
        cta = cta_m.group(1).strip()

    # Extract CPM ($X per 1,000 views)
    cpm = 1.0
    cpm_m = re.search(r"CPM:\s*\$\s*([0-9]+(?:\.[0-9]+)?)", raw, re.I)
    if cpm_m:
        cpm = float(cpm_m.group(1))

    # Extract Max Payout ($X per clip)
    max_pay = 300.0
    mp_m = re.search(r"Max\s+Payout:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)", raw, re.I)
    if mp_m:
        max_pay = float(mp_m.group(1).replace(",", ""))

    # Extract Min Engagement Rate (X%)
    eng = 0.8
    eng_m = re.search(r"([0-9]+(?:\.[0-9]+)?)%\s+engagement\s+rate", raw, re.I)
    if eng_m:
        eng = float(eng_m.group(1))

    # Extract Stream URLs (Twitch / YouTube)
    urls = []
    for um in re.finditer(r"https?://(?:www\.)?(?:twitch\.tv|youtube\.com|youtu\.be)/[^\s)\]]+", raw, re.I):
        u = um.group(0).rstrip(".,;")
        if u not in urls:
            urls.append(u)

    no_links = bool(re.search(r"do not include links|no links or urls", raw, re.I))
    req_ov = bool(re.search(r"logo overlay|overlay must|put the .* logo", raw, re.I))

    first_word = name.split()[0] if name else "Streamer"
    if first_word.lower() in ("put", "add", "use", "clip", "turn", "make", "post", "join", "the"):
        streamer = next((t for t in tags if t.lower() not in ("shorts", "clips", "viral")), "GTA6")
        req_cta = cta
    else:
        streamer = first_word
        req_cta = cta or f"Watch {streamer} on YouTube and Twitch! 🔴"
    return {
        "name": name[:60],
        "streamer": streamer,
        "stream_urls": urls,
        "required_cta": req_cta,
        "required_hashtags": tags or ["shorts", "gaming"],
        "no_links": no_links,
        "require_overlay": req_ov,
        "cpm_usd": cpm,
        "max_payout_usd": max_pay,
        "min_engagement_pct": eng,
        "cliffhanger_enabled": True,
        "cliffhanger_template": "WAIT FOR THE END... 😱",
        "cliffhanger_duration": 4.0,
        "notes": f"Parsed from brief ({len(raw)} chars).",
    }

