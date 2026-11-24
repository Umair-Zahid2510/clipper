---
name: clipper-io
description: >-
  Turn long YouTube videos, local recordings, or live YouTube/Twitch streams
  into vertical 9:16 viral Shorts, TikToks, and Reels with animated captions,
  campaign compliance, and jittered multi-social auto-posting. Use when the user
  wants to clip videos, monitor live streams, render vertical shorts, or
  auto-post to YouTube Shorts, TikTok, or Instagram Reels.
---

# clipper.io (Clipsmith) Agent Skill

Automate end-to-end short-form video clipping, vertical 9:16 rendering, campaign rule compliance, and jittered multi-social publishing via the local `clipper.io` FastAPI server (`http://127.0.0.1:8765`).

## Prerequisites & Setup

- **Runtime**: Python 3.10+, `ffmpeg` + `ffprobe` on `PATH`, `yt-dlp` (installed via `requirements.txt`), and Deno or Node.js for YouTube downloads.
- **Optional AI Providers** (configured in `.env`):
  - `AI_PROVIDER=ollama` (`qwen3.5:4b` local)
  - `AI_PROVIDER=gemini` (`GEMINI_API_KEY`)
  - `AI_PROVIDER=claude` (`ANTHROPIC_API_KEY`)
  - `AI_PROVIDER=none` (fast deterministic clipper captions & scoring)
- **Install**:
  - Windows: `setup.bat`
  - macOS / Linux: `./setup.sh`

## Start the Server

```bash
# Windows
run.bat
# macOS / Linux
./run.sh
# Or directly:
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Verify the server is reachable before issuing API calls:

```bash
curl -s http://127.0.0.1:8765/api/posts
```

## Core Workflows

### 1. Clip a YouTube Video or Local File
- Open `http://127.0.0.1:8765/` or submit via `POST /api/jobs` with `url`, `count`, `min_len`, `max_len`, `aspect` (`9:16`, `1:1`, `16:9`), and `style`.
- Download all rendered clips as a ZIP via `GET /api/jobs/{job_id}/zip`.

### 2. Monitor Live Streams & Catch-Up Backfill
- Open `http://127.0.0.1:8765/watcher` to start monitoring a YouTube Live or Twitch channel (`POST /api/watcher/start`).
- Exported live clips support campaign logo overlays, `-14 LUFS` loudness normalization, clean emoji-free white hook banners, and one-click ZIP export via `GET /api/watcher/zip`.

### 3. Manage Campaigns & Compliance Rules
- Open `http://127.0.0.1:8765/campaigns` (or `GET/POST /api/campaigns`) to set required hashtags, CTAs, link stripping (`no_links`), and logo overlays.

### 4. Approve & Auto-Post Across YouTube, TikTok & Instagram
- Open `http://127.0.0.1:8765/posts` to connect social accounts (YouTube OAuth, TikTok PKCE OAuth with 24h auto-refresh, Instagram Graph API).
- Approve clips (`POST /api/posts/approve`) and start the sequential loop (`POST /api/posts/loop`) with anti-block jitter (`jitter_pct`, default `30`), per-platform rate-limit cooldowns, optional `best_first` / `max_posts`, and opt-in `auto_wait_quota`.

## Known Platform Limits
- **YouTube Data API v3**: `videos.insert` has a 100 calls/day bucket (resetting midnight PT). Enable `auto_wait_quota: true` to sleep and auto-resume cleanly.
- **TikTok Content Posting API v2 (Unaudited/Sandbox)**: Public accounts fall back to Inbox Drafts (`MEDIA_UPLOAD`, capped at ~5 unpublished drafts in the TikTok mobile app Inbox). Clear pending drafts in **TikTok App -> Inbox -> System Notifications**, then resume the loop.
