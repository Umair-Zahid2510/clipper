"""Multi-social account setup (YouTube Shorts, TikTok, Instagram Reels), platform-specific
payload builders, direct uploaders, approval queue, and sequential auto-post loop."""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import requests

from . import campaigns, config, youtube

SOCIAL_ACCOUNTS_PATH = config.DATA_DIR / "social_accounts.json"
PLATFORMS = ("youtube", "tiktok", "instagram")
APPROVAL_STATES = ("pending", "approved", "rejected")

_store_lock = threading.Lock()
_tt_rate_lock = threading.Lock()
_last_tt_init_ts: float = 0.0


# ---------- Token & Account Setup Store ----------

def _read_social_store() -> dict:
    with _store_lock:
        try:
            data = json.loads(SOCIAL_ACCOUNTS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("tiktok", {"active_id": None, "accounts": {}})
                data.setdefault("instagram", {"active_id": None, "accounts": {}})
                return data
        except (OSError, ValueError):
            pass
        return {
            "tiktok": {"active_id": None, "accounts": {}},
            "instagram": {"active_id": None, "accounts": {}},
        }


def _write_social_store(store: dict) -> None:
    with _store_lock:
        SOCIAL_ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SOCIAL_ACCOUNTS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SOCIAL_ACCOUNTS_PATH)


def save_youtube_client_credentials(client_id: str, client_secret: str) -> dict:
    """Save YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to .env and activate immediately in memory."""
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid or not secret:
        raise ValueError("Both YouTube Client ID and Client Secret are required.")

    os.environ["YOUTUBE_CLIENT_ID"] = cid
    os.environ["YOUTUBE_CLIENT_SECRET"] = secret

    env_path = config.ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    out_lines: list[str] = []
    seen_id = seen_sec = False
    for line in lines:
        if re.match(r"^\s*#?\s*YOUTUBE_CLIENT_ID\s*=", line):
            out_lines.append(f"YOUTUBE_CLIENT_ID={cid}")
            seen_id = True
        elif re.match(r"^\s*#?\s*YOUTUBE_CLIENT_SECRET\s*=", line):
            out_lines.append(f"YOUTUBE_CLIENT_SECRET={secret}")
            seen_sec = True
        else:
            out_lines.append(line)
    if not seen_id:
        out_lines.append(f"YOUTUBE_CLIENT_ID={cid}")
    if not seen_sec:
        out_lines.append(f"YOUTUBE_CLIENT_SECRET={secret}")
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return all_accounts_status()


def save_tiktok_client_credentials(
    client_key: str,
    client_secret: str,
    redirect_uri: str | None = None,
) -> dict:
    """Save TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, and optional TIKTOK_REDIRECT_URI to .env and activate immediately in memory."""
    ck = (client_key or "").strip()
    cs = (client_secret or "").strip()
    ru = (redirect_uri or os.environ.get("TIKTOK_REDIRECT_URI") or "https://github.com/Umair-Zahid2510/clipper").strip()
    if not ck or not cs:
        raise ValueError("Both TikTok Client Key and Client Secret are required.")

    os.environ["TIKTOK_CLIENT_KEY"] = ck
    os.environ["TIKTOK_CLIENT_SECRET"] = cs
    os.environ["TIKTOK_REDIRECT_URI"] = ru

    env_path = config.ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    out_lines: list[str] = []
    seen_k = seen_s = seen_r = False
    for line in lines:
        if re.match(r"^\s*#?\s*TIKTOK_CLIENT_KEY\s*=", line):
            out_lines.append(f"TIKTOK_CLIENT_KEY={ck}")
            seen_k = True
        elif re.match(r"^\s*#?\s*TIKTOK_CLIENT_SECRET\s*=", line):
            out_lines.append(f"TIKTOK_CLIENT_SECRET={cs}")
            seen_s = True
        elif re.match(r"^\s*#?\s*TIKTOK_REDIRECT_URI\s*=", line):
            out_lines.append(f"TIKTOK_REDIRECT_URI={ru}")
            seen_r = True
        else:
            out_lines.append(line)
    if not seen_k:
        out_lines.append(f"TIKTOK_CLIENT_KEY={ck}")
    if not seen_s:
        out_lines.append(f"TIKTOK_CLIENT_SECRET={cs}")
    if not seen_r:
        out_lines.append(f"TIKTOK_REDIRECT_URI={ru}")
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return all_accounts_status()


_tt_pending: dict[str, dict] = {}
_tt_last_state: str | None = None


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate RFC 7636 PKCE (code_verifier, S256 code_challenge) required by TikTok Desktop/Web OAuth v2."""
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def start_tiktok_auth(
    redirect_uri: str | None = None,
    handle_hint: str = "@clipcurrent1000",
) -> str:
    """Build the TikTok OAuth v2 authorization URL with PKCE (code_challenge & code_challenge_method=S256)."""
    global _tt_last_state
    from urllib.parse import urlencode

    ck = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    cs = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    if not ck or not cs:
        raise ValueError("Save your TikTok Sandbox/App Client Key & Client Secret first.")
    final_redirect = (
        (redirect_uri or "").strip()
        or os.environ.get("TIKTOK_REDIRECT_URI", "").strip()
        or "https://github.com/Umair-Zahid2510/clipper"
    )
    verifier, challenge = _generate_pkce_pair()
    state = f"tt_{uuid.uuid4().hex[:18]}"
    _tt_pending[state] = {
        "client_key": ck,
        "client_secret": cs,
        "redirect_uri": final_redirect,
        "code_verifier": verifier,
        "code_challenge": challenge,
        "handle_hint": (handle_hint or "@clipcurrent1000").strip(),
        "created": time.time(),
    }
    _tt_last_state = state
    params = {
        "client_key": ck,
        "scope": "user.info.basic,video.publish,video.upload",
        "response_type": "code",
        "redirect_uri": final_redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(params)}"


def finish_tiktok_auth(
    state: str | None,
    code_or_url: str,
    handle_override: str | None = None,
    redirect_uri_override: str | None = None,
) -> dict:
    """Exchange TikTok authorization code (or full redirected URL containing ?code=...) with PKCE code_verifier."""
    from urllib.parse import parse_qs, unquote, urlparse

    raw = (code_or_url or "").strip()
    if not raw:
        raise ValueError("Paste the redirected URL (or authorization code) from TikTok.")

    extracted_code = raw
    extracted_state = (state or "").strip()
    if "://" in raw or "code=" in raw:
        parsed = urlparse(raw if "://" in raw else f"https://example.com/?{raw.lstrip('?')}")
        qs = parse_qs(parsed.query)
        if qs.get("code"):
            extracted_code = unquote(qs["code"][0])
        if not extracted_state and qs.get("state"):
            extracted_state = qs["state"][0]

    pending = _tt_pending.pop(extracted_state, None)
    if pending is None and _tt_last_state and _tt_last_state in _tt_pending:
        pending = _tt_pending.pop(_tt_last_state, None)

    ck = (pending or {}).get("client_key") or os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    cs = (pending or {}).get("client_secret") or os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    redirect_uri = (
        (redirect_uri_override or "").strip()
        or (pending or {}).get("redirect_uri")
        or os.environ.get("TIKTOK_REDIRECT_URI", "").strip()
        or "https://github.com/Umair-Zahid2510/clipper"
    )
    code_verifier = (pending or {}).get("code_verifier") or ""
    handle = (handle_override or "").strip() or (pending or {}).get("handle_hint") or "@clipcurrent1000"

    token_payload = {
        "client_key": ck,
        "client_secret": cs,
        "code": extracted_code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        token_payload["code_verifier"] = code_verifier

    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=token_payload,
        timeout=30,
    )
    data = resp.json() if resp.content else {}
    access_token = data.get("access_token")
    if (resp.status_code >= 400 or not access_token) and code_verifier:
        # Retry once without code_verifier in case the TikTok app platform is Web-only
        token_payload.pop("code_verifier", None)
        resp = requests.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=token_payload,
            timeout=30,
        )
        data = resp.json() if resp.content else {}
        access_token = data.get("access_token")

    if resp.status_code >= 400 or not access_token:
        err_msg = data.get("error_description") or data.get("message") or data.get("error") or f"HTTP {resp.status_code}"
        raise RuntimeError(f"TikTok OAuth token exchange failed: {err_msg}")

    # Try fetching display_name/username from user.info.basic if available
    try:
        u_resp = requests.get(
            "https://open.tiktokapis.com/v2/user/info/",
            params={"fields": "open_id,display_name,username"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        u_data = ((u_resp.json() if u_resp.content else {}).get("data") or {}).get("user") or {}
        uname = u_data.get("username") or u_data.get("display_name")
        if uname:
            handle = f"@{uname.lstrip('@')}"
    except Exception:
        pass

    return add_social_account(
        platform="tiktok",
        title=handle,
        access_token=access_token,
        open_id=data.get("open_id"),
        refresh_token=data.get("refresh_token"),
        client_key=ck,
        client_secret=cs,
    )


def refresh_tiktok_account_token(account_id: str | None = None) -> dict:
    """Automatically refresh a 24-hour TikTok access_token using its stored refresh_token."""
    store = _read_social_store()
    aid = account_id or store["tiktok"].get("active_id")
    acc = (store["tiktok"]["accounts"].get(aid) if aid else None)
    if not acc:
        raise RuntimeError("TikTok account not found.")
    rft = (acc.get("refresh_token") or "").strip()
    ck = (acc.get("client_key") or os.environ.get("TIKTOK_CLIENT_KEY", "")).strip()
    cs = (acc.get("client_secret") or os.environ.get("TIKTOK_CLIENT_SECRET", "")).strip()
    if not rft or not ck or not cs:
        raise RuntimeError("Cannot auto-refresh TikTok token: save Refresh Token, Client Key, and Client Secret in TikTok Setup.")

    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": ck,
            "client_secret": cs,
            "grant_type": "refresh_token",
            "refresh_token": rft,
        },
        timeout=30,
    )
    data = resp.json() if resp.content else {}
    new_tok = data.get("access_token")
    if resp.status_code >= 400 or not new_tok:
        err_msg = data.get("error_description") or data.get("error") or f"HTTP {resp.status_code}"
        raise RuntimeError(f"TikTok token refresh failed: {err_msg}")

    acc["access_token"] = new_tok
    if data.get("refresh_token"):
        acc["refresh_token"] = data["refresh_token"]
    if data.get("open_id"):
        acc["open_id"] = data["open_id"]
    acc["client_key"] = ck
    acc["client_secret"] = cs
    acc["refreshed_at"] = time.time()
    store["tiktok"]["accounts"][aid] = acc
    _write_social_store(store)
    return acc


def add_social_account(
    platform: str,
    title: str,
    access_token: str,
    ig_user_id: str | None = None,
    open_id: str | None = None,
    refresh_token: str | None = None,
    client_key: str | None = None,
    client_secret: str | None = None,
) -> dict:
    """Add or update a TikTok or Instagram Reels account token directly from the dashboard."""
    plat = (platform or "").strip().lower()
    if plat not in ("tiktok", "instagram"):
        raise ValueError("Platform must be 'tiktok' or 'instagram'.")
    tok = (access_token or "").strip()
    label = (title or "").strip()
    if not tok:
        raise ValueError("Access Token is required.")
    if not label:
        raise ValueError("Account handle or label is required (e.g. @kreekclips).")

    store = _read_social_store()
    if plat == "instagram":
        uid = (ig_user_id or "").strip()
        if not uid:
            raise ValueError("Instagram Business/Creator User ID (ig_user_id) is required.")
        aid = f"ig_{uid}"
        entry = {
            "id": aid,
            "platform": "instagram",
            "title": label,
            "ig_user_id": uid,
            "access_token": tok,
            "created": time.time(),
        }
    else:
        oid = (open_id or "").strip() or uuid.uuid4().hex[:8]
        aid = f"tt_{re.sub(r'[^a-zA-Z0-9_]', '', label.lstrip('@')) or oid}"
        entry = {
            "id": aid,
            "platform": "tiktok",
            "title": label,
            "open_id": oid,
            "access_token": tok,
            "refresh_token": (refresh_token or "").strip() or None,
            "client_key": (client_key or "").strip() or None,
            "client_secret": (client_secret or "").strip() or None,
            "created": time.time(),
        }

    store[plat]["accounts"][aid] = entry
    store[plat]["active_id"] = aid
    _write_social_store(store)
    return all_accounts_status()


def select_social_account(platform: str, account_id: str) -> dict:
    plat = (platform or "").strip().lower()
    if plat == "youtube":
        youtube.select_account(account_id)
        return all_accounts_status()
    if plat not in ("tiktok", "instagram"):
        raise ValueError("Unknown platform.")
    store = _read_social_store()
    if account_id not in store[plat]["accounts"]:
        raise ValueError("Account not found.")
    store[plat]["active_id"] = account_id
    _write_social_store(store)
    return all_accounts_status()


def remove_social_account(platform: str, account_id: str | None = None) -> dict:
    plat = (platform or "").strip().lower()
    if plat == "youtube":
        if account_id:
            youtube.disconnect(account_id)
        else:
            youtube.disconnect()
        return all_accounts_status()
    if plat not in ("tiktok", "instagram"):
        raise ValueError("Unknown platform.")
    store = _read_social_store()
    target = account_id or store[plat].get("active_id")
    if target and target in store[plat]["accounts"]:
        del store[plat]["accounts"][target]
    rem = list(store[plat]["accounts"].keys())
    store[plat]["active_id"] = rem[0] if rem else None
    _write_social_store(store)
    return all_accounts_status()


def get_account_credentials(platform: str, account_id: str | None = None) -> dict | None:
    store = _read_social_store()
    if platform not in ("tiktok", "instagram"):
        return None
    aid = account_id or store[platform].get("active_id")
    if not aid:
        return None
    return store[platform]["accounts"].get(aid)


def all_accounts_status() -> dict:
    yt_acc = youtube.list_accounts()
    yt_st = youtube.status()
    store = _read_social_store()

    def _fmt_plat(plat: str) -> dict:
        pstore = store.get(plat) or {"active_id": None, "accounts": {}}
        active_id = pstore.get("active_id")
        accs = []
        for aid, info in (pstore.get("accounts") or {}).items():
            accs.append({
                "id": aid,
                "title": info.get("title") or aid,
                "ig_user_id": info.get("ig_user_id"),
                "active": aid == active_id,
            })
        return {
            "configured": len(accs) > 0,
            "connected": len(accs) > 0,
            "active_id": active_id,
            "accounts": accs,
        }

    return {
        "youtube": {
            "configured": yt_acc["configured"],
            "connected": yt_st["connected"],
            "active_id": yt_acc["active_id"],
            "channel": yt_st.get("channel"),
            "accounts": yt_acc["accounts"],
            "client_id_set": bool(os.environ.get("YOUTUBE_CLIENT_ID", "").strip()),
        },
        "tiktok": {
            **_fmt_plat("tiktok"),
            "client_key_set": bool(os.environ.get("TIKTOK_CLIENT_KEY", "").strip()),
            "redirect_uri": os.environ.get("TIKTOK_REDIRECT_URI", "https://github.com/Umair-Zahid2510/clipper").strip(),
        },
        "instagram": _fmt_plat("instagram"),
    }


_URL_RE = re.compile(r"https?://\S+|www\.\S+|(?:twitch\.tv|youtube\.com|youtu\.be|discord\.gg)/\S*", re.I)
REQUIRED_CTA = "Watch KreekCraft on YouTube and Twitch"
REQUIRED_TAGS = ("GTA6", "KreekCraft", "GrandTheftAutoVI")


def enforce_campaign_caption(text: str, hashtags: list[str] | None = None, campaign_rules: dict | None = None) -> str:
    """Format a social caption according to the active campaign rules (or leave standard if General Mode)."""
    rules = campaign_rules or campaigns.get_campaign()
    is_camp = rules.get("id") != "general"
    no_links = bool(rules.get("no_links", True)) if is_camp else False
    req_cta = (rules.get("required_cta") if "required_cta" in rules else REQUIRED_CTA).strip() if is_camp else ""
    req_tags = list(rules.get("required_hashtags") or REQUIRED_TAGS) if is_camp else []

    cleaned = _URL_RE.sub("", text or "").strip() if no_links else (text or "").strip()
    if req_cta:
        core_cta = re.sub(r"[^\w\s]+$", "", req_cta).strip().lower()
        if core_cta and core_cta not in cleaned.lower():
            cleaned = f"{cleaned} {req_cta}".strip()
    tags = [str(t).lstrip("#").strip() for t in (hashtags or req_tags or ["shorts"]) if str(t).strip()]
    for req in reversed(req_tags):
        if req.lower() not in [t.lower() for t in tags]:
            tags.insert(0, req)
    missing_tag_strs = [f"#{t.replace(' ', '')}" for t in tags if f"#{t.lower()}" not in cleaned.lower()]
    if missing_tag_strs:
        cleaned = f"{cleaned}\n\n{' '.join(missing_tag_strs)}".strip()
    return cleaned[:2200]


def enforce_kreekcraft_caption(text: str, hashtags: list[str] | None = None) -> str:
    return enforce_campaign_caption(text, hashtags)


# ---------- Platform-Specific Payload Builders ----------

def build_platform_payloads(item: dict) -> dict[str, dict]:
    """Build the exact API request payload for YouTube Shorts, TikTok, and Instagram Reels."""
    rules = campaigns.get_campaign(item.get("campaign_id"))
    is_camp = rules.get("id") != "general"
    no_links = bool(rules.get("no_links", True)) if is_camp else False
    req_cta = (rules.get("required_cta") if "required_cta" in rules else REQUIRED_CTA).strip() if is_camp else ""
    req_tags = list(rules.get("required_hashtags") or REQUIRED_TAGS) if is_camp else ["shorts"]

    raw_title = (item.get("title") or "Clip Highlight").strip()
    title = _URL_RE.sub("", raw_title).strip() if no_links else raw_title
    raw_desc = (item.get("description") or req_cta).strip()
    if no_links:
        raw_desc = _URL_RE.sub("", raw_desc).strip()
    if req_cta:
        core_cta = re.sub(r"[^\w\s]+$", "", req_cta).strip().lower()
        if core_cta and core_cta not in raw_desc.lower():
            raw_desc = f"{raw_desc}\n\n{req_cta}".strip()
    tags = [str(t).lstrip("#").strip() for t in (item.get("hashtags") or req_tags) if str(t).strip()]
    for req in reversed(req_tags if is_camp else []):
        if req.lower() not in [t.lower() for t in tags]:
            tags.insert(0, req)

    # Combined social caption for TikTok & Instagram Reels (where caption + hashtags live in one field)
    combined_caption = enforce_campaign_caption(raw_desc, tags, rules)

    # 1. YouTube Shorts payload
    yt_priv = item.get("privacy") or "public"
    if yt_priv not in youtube.PRIVACY:
        yt_priv = "public"
    src_url = None if no_links else (item.get("source_url") if item.get("add_source_link") else None)
    try:
        yt_meta = youtube.build_metadata(
            title=title,
            description=raw_desc,
            hashtags=tags,
            privacy=yt_priv,
            made_for_kids=bool(item.get("made_for_kids", False)),
            source_url=src_url,
        )
    except ValueError:
        yt_meta = {"snippet": {"title": title[:100], "description": combined_caption, "tags": tags}, "status": {"privacyStatus": yt_priv, "selfDeclaredMadeForKids": False}}

    yt_payload = {
        "platform": "youtube",
        "endpoint": "POST https://www.googleapis.com/upload/youtube/v3/videos?part=snippet,status",
        "account_id": item.get("account_id"),
        "notify_subscribers": bool(item.get("notify_subscribers", True)),
        "body": yt_meta,
    }

    # 2. TikTok Content Posting API v2 payload
    tt_opts = item.get("tiktok_options") or {}
    tt_privacy = tt_opts.get("privacy_level") or "PUBLIC_TO_EVERYONE"
    tt_mode = tt_opts.get("post_mode") or "DIRECT_POST"
    tt_caption = enforce_campaign_caption(tt_opts.get("caption") or combined_caption, tags, rules)
    tt_endpoint = (
        "POST https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
        if tt_mode == "MEDIA_UPLOAD"
        else "POST https://open.tiktokapis.com/v2/post/publish/video/init/"
    )
    tt_payload = {
        "platform": "tiktok",
        "endpoint": tt_endpoint,
        "account_id": tt_opts.get("account_id"),
        "post_mode": tt_mode,
        "body": {
            "post_info": {
                "title": tt_caption,
                "privacy_level": tt_privacy,
                "disable_duet": not bool(tt_opts.get("allow_duet", True)),
                "disable_comment": not bool(tt_opts.get("allow_comment", True)),
                "disable_stitch": not bool(tt_opts.get("allow_stitch", True)),
                "video_cover_timestamp_ms": int(tt_opts.get("cover_timestamp_ms", 1000)),
            },
            "source_info": {
                "source": "FILE_UPLOAD",
            },
        },
    }

    # 3. Instagram Graph API v21.0 Reels Resumable Upload payload
    ig_opts = item.get("instagram_options") or {}
    ig_acc = get_account_credentials("instagram", ig_opts.get("account_id"))
    ig_uid = (ig_acc or {}).get("ig_user_id") or "{ig_user_id}"
    ig_caption = enforce_campaign_caption(ig_opts.get("caption") or combined_caption, tags, rules)
    ig_payload = {
        "platform": "instagram",
        "endpoint": f"POST https://graph.facebook.com/v21.0/{ig_uid}/media",
        "account_id": ig_opts.get("account_id"),
        "body": {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": ig_caption,
            "share_to_feed": bool(ig_opts.get("share_to_feed", True)),
            "audio_name": (ig_opts.get("audio_name") or "KreekCraft GTA 6 Live").strip()[:100],
            "thumb_offset": int(ig_opts.get("thumb_offset_ms", 1000)),
        },
    }

    return {
        "youtube": yt_payload,
        "tiktok": tt_payload,
        "instagram": ig_payload,
    }


# ---------- Direct Uploaders for TikTok & Instagram Reels ----------

def upload_to_tiktok(
    video_path: Path,
    payload: dict,
    account_id: str | None = None,
    progress: Callable[[int], None] | None = None,
) -> dict:
    """Upload MP4 directly to TikTok via Content Posting API v2 (FILE_UPLOAD) with automatic 24h token refresh and Sandbox SELF_ONLY fallback."""
    acc = get_account_credentials("tiktok", account_id)
    if not acc or not acc.get("access_token"):
        raise RuntimeError("TikTok account not connected. Add your TikTok Access Token in Social Setup first.")

    # Proactively auto-refresh if token is > 23 hours old and refresh_token is stored
    age = time.time() - float(acc.get("refreshed_at") or acc.get("created") or 0)
    if age > 23 * 3600 and acc.get("refresh_token") and (acc.get("client_key") or os.environ.get("TIKTOK_CLIENT_KEY")):
        try:
            acc = refresh_tiktok_account_token(acc.get("id"))
        except Exception:
            pass

    size = video_path.stat().st_size
    body = json.loads(json.dumps(payload["body"]))
    body["source_info"] = {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": size,
        "total_chunk_count": 1,
    }
    force_inbox = bool(acc.get("force_inbox_draft")) and (
        time.time() - float(acc.get("force_inbox_draft_at") or 0) < 86400
    )
    posted_mode = "MEDIA_UPLOAD" if (payload.get("post_mode") == "MEDIA_UPLOAD" or force_inbox) else (payload.get("post_mode") or "DIRECT_POST")
    init_url = (
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
        if posted_mode == "MEDIA_UPLOAD"
        else "https://open.tiktokapis.com/v2/post/publish/video/init/"
    )
    init_body = {"source_info": body["source_info"]} if posted_mode == "MEDIA_UPLOAD" else body
    if progress:
        progress(10)

    def _call_init(url: str, token: str, req_body: dict):
        global _last_tt_init_ts
        with _tt_rate_lock:
            min_gap = 15.0 + random.uniform(2.0, 7.0)
            wait = min_gap - (time.monotonic() - _last_tt_init_ts)
            if wait > 0:
                time.sleep(wait)
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json=req_body,
                timeout=30,
            )
            _last_tt_init_ts = time.monotonic()
        d = r.json() if r.content else {}
        return r, d

    resp, data = _call_init(init_url, acc["access_token"], init_body)
    err = data.get("error") or {}
    err_code = str(err.get("code") or "").lower()

    # 1. If 401 / access_token_invalid, automatically refresh token and retry once
    if (resp.status_code == 401 or "token" in err_code) and acc.get("refresh_token"):
        acc = refresh_tiktok_account_token(acc.get("id"))
        resp, data = _call_init(init_url, acc["access_token"], init_body)
        err = data.get("error") or {}
        err_code = str(err.get("code") or "").lower()

    # 2. If TikTok Sandbox / Unaudited app rejects PUBLIC_TO_EVERYONE, try SELF_ONLY, and if the account is public
    # (`unaudited_client_can_only_post_to_private_accounts`), automatically fall back to Inbox Draft (`MEDIA_UPLOAD`)
    # and cache `force_inbox_draft` for 24h so subsequent clips take 1 init call instead of 3.
    if posted_mode != "MEDIA_UPLOAD" and (resp.status_code >= 400 or (err_code and err_code != "ok")) and ("unaudited" in err_code or "privacy" in err_code or "content-sharing-guidelines" in str(err.get("message", "")).lower()):
        if body.get("post_info", {}).get("privacy_level") != "SELF_ONLY":
            body.setdefault("post_info", {})["privacy_level"] = "SELF_ONLY"
            resp, data = _call_init(init_url, acc["access_token"], body)
            err = data.get("error") or {}
            err_code = str(err.get("code") or "").lower()

        if (resp.status_code >= 400 or (err_code and err_code != "ok")) and ("unaudited" in err_code or "content-sharing-guidelines" in str(err.get("message", "")).lower()):
            inbox_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
            posted_mode = "MEDIA_UPLOAD"
            resp, data = _call_init(inbox_url, acc["access_token"], {"source_info": body["source_info"]})
            err = data.get("error") or {}
            err_code = str(err.get("code") or "").lower()
            try:
                store = _read_social_store()
                aid = acc.get("id") or store["tiktok"].get("active_id")
                if aid and aid in store["tiktok"]["accounts"]:
                    store["tiktok"]["accounts"][aid]["force_inbox_draft"] = True
                    store["tiktok"]["accounts"][aid]["force_inbox_draft_at"] = time.time()
                    _write_social_store(store)
            except Exception:
                pass

    if resp.status_code >= 400 or (err_code and err_code != "ok"):
        if "spam_risk_too_many_pending_share" in err_code or "pending_share" in err_code:
            raise RuntimeError(
                "TikTok Inbox draft limit reached (spam_risk_too_many_pending_share): "
                "TikTok allows at most ~5 unpublished drafts in your App Inbox. "
                "Open the TikTok mobile app on this account (Inbox -> System Notifications) to publish or clear pending drafts, then click Resume."
            )
        msg = err.get("message") or data.get("message") or f"HTTP {resp.status_code}"
        if resp.status_code == 429 or "rate_limit" in err_code:
            raise RuntimeError(f"TikTok API rejected init (rate_limit_exceeded): {msg}")
        raise RuntimeError(f"TikTok API rejected init ({err_code or resp.status_code}): {msg}")

    d = data.get("data") or {}
    publish_id = d.get("publish_id") or "tt_pub"
    upload_url = d.get("upload_url")
    if not upload_url:
        raise RuntimeError("TikTok API did not return an upload_url.")

    if progress:
        progress(35)
    with open(video_path, "rb") as fh:
        put_resp = requests.put(
            upload_url,
            headers={
                "Content-Range": f"bytes 0-{size - 1}/{size}",
                "Content-Type": "video/mp4",
            },
            data=fh,
            timeout=300,
        )
    if put_resp.status_code >= 400:
        raise RuntimeError(f"TikTok video upload failed (HTTP {put_resp.status_code}).")
    if progress:
        progress(100)
    handle = acc.get("title", "").lstrip("@")
    res = {
        "status": "done",
        "publish_id": publish_id,
        "post_mode": posted_mode,
        "account": acc.get("title"),
        "url": f"https://www.tiktok.com/@{handle}" if handle else "https://www.tiktok.com/",
    }
    if posted_mode == "MEDIA_UPLOAD":
        res["note"] = "Sent to TikTok App Inbox (open TikTok app notifications to publish)"
    return res


def upload_to_instagram(
    video_path: Path,
    payload: dict,
    account_id: str | None = None,
    progress: Callable[[int], None] | None = None,
) -> dict:
    """Upload MP4 directly to Instagram Reels via Meta Graph API v21.0 Resumable Upload."""
    acc = get_account_credentials("instagram", account_id)
    if not acc or not acc.get("access_token") or not acc.get("ig_user_id"):
        raise RuntimeError("Instagram account not connected. Add your IG User ID and Access Token in Social Setup first.")

    ig_uid = acc["ig_user_id"]
    token = acc["access_token"]
    size = video_path.stat().st_size
    body = dict(payload["body"])

    if progress:
        progress(10)
    init_resp = requests.post(
        f"https://graph.facebook.com/v21.0/{ig_uid}/media",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=30,
    )
    init_data = init_resp.json() if init_resp.content else {}
    if init_resp.status_code >= 400 or "id" not in init_data:
        err_msg = (init_data.get("error") or {}).get("message") or f"HTTP {init_resp.status_code}"
        raise RuntimeError(f"Instagram container init failed: {err_msg}")

    container_id = init_data["id"]
    upload_uri = init_data.get("uri") or f"https://rupload.facebook.com/ig-api-upload/v21.0/{container_id}"

    if progress:
        progress(35)
    with open(video_path, "rb") as fh:
        up_resp = requests.post(
            upload_uri,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
            },
            data=fh,
            timeout=300,
        )
    if up_resp.status_code >= 400:
        raise RuntimeError(f"Instagram Reels binary upload failed (HTTP {up_resp.status_code}).")

    if progress:
        progress(75)
    # Wait for container processing
    for _ in range(30):
        st_resp = requests.get(
            f"https://graph.facebook.com/v21.0/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=20,
        )
        st_data = st_resp.json() if st_resp.content else {}
        code = st_data.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram Reels processing failed: {st_data.get('status') or code}")
        time.sleep(2.0)

    pub_resp = requests.post(
        f"https://graph.facebook.com/v21.0/{ig_uid}/media_publish",
        params={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    pub_data = pub_resp.json() if pub_resp.content else {}
    if pub_resp.status_code >= 400 or "id" not in pub_data:
        err_msg = (pub_data.get("error") or {}).get("message") or f"HTTP {pub_resp.status_code}"
        raise RuntimeError(f"Instagram Reels publish failed: {err_msg}")

    if progress:
        progress(100)
    handle = acc.get("title", "").lstrip("@")
    return {
        "status": "done",
        "media_id": pub_data["id"],
        "account": acc.get("title"),
        "url": f"https://www.instagram.com/{handle}/reels/" if handle else "https://www.instagram.com/",
    }

