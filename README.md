<p align="center">
  <img src="docs/logo.png" alt="clipper.io logo" width="120" height="120" />
</p>

<h1 align="center">clipper.io (Clipsmith)</h1>

**A free, open-source, self-hosted alternative to OpusClip. Turn long YouTube videos, local files, or live YouTube & Twitch streams into ready-to-post Shorts, TikToks, and Reels — with automated campaign compliance and multi-social publishing.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Runs local](https://img.shields.io/badge/runs-local-brightgreen)
![No GPU](https://img.shields.io/badge/GPU-not%20needed-brightgreen)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-SKILL.md-blueviolet)](SKILL.md)

> Not affiliated with OpusClip. "OpusClip" is mentioned only as a reference point.

![Clipsmith screenshot](docs/ss.png)

## Why clipper.io?

| Feature | clipper.io | OpusClip Free | OpusClip Paid |
|---|---|---|---|
| Price | **Free & Open Source (MIT)** | $0 | From $15/mo |
| Clips per month | **Unlimited** | 60 credits | 150+ credits |
| Watermark | **None** | Yes | No |
| Live Stream Catch-Up Watcher | **YouTube Live + Twitch** | No | Limited |
| Multi-Social Auto-Posting | **YouTube + TikTok + IG Reels** | Limited | Yes |
| Campaign Rules & Compliance Studio | **Built-in** | No | No |
| Runs locally on your machine | **Yes** | No | No |

## Core Features

1. **AI & Heuristic Clip Selection (`/`)**: Downloads YouTube videos (or accepts local files), transcribes with Whisper when captions are absent, picks high-retention moments, and learns from past `live_stats` (`views`, `engagement`) when an AI provider is enabled.
2. **Pro Vertical Editing**:
   - **9:16 Face-Tracking Reframe** (split facecam + gameplay or blurred background fit).
   - **Animated Word-by-Word Pop Captions** (`build_ass`) + clean emoji-free white hook banners.
   - **Broadcast Audio**: `-14 LUFS` `loudnorm` loudness normalization and smooth fade-in.
3. **Live Stream Watcher & Parallel Catch-Up (`/watcher`)**: Monitors live YouTube/Twitch streams for chat hype spikes, backfills missed stream hours concurrently, and exports ZIP batches via `GET /api/watcher/zip`.
4. **Campaigns & Rules Studio (`/campaigns`)**: Enforce required CTAs, hashtags, link stripping, and PNG/MOV/GIF logo overlays with real-time compliance badges.
5. **Multi-Social Publishing & Anti-Block Loop (`/posts`)**:
   - **YouTube Shorts**: OAuth + multi-channel rotation + live view/like/comment tracker.
   - **TikTok**: Built-in PKCE (`S256`) OAuth, 24h auto-refresh, 24h Sandbox Inbox Draft (`MEDIA_UPLOAD`) caching, and 15s+ jittered `init` pacing.
   - **Instagram Reels**: Graph API v21.0 resumable upload.
   - **Graceful Sequential Loop**: `±30%` anti-block jitter, `65–95s` per-platform rate-limit cooldowns, `best_first` / `max_posts` targeting, and opt-in `Auto-wait on quota reset`.

## Use as an AI Agent Skill

This repository includes a standard [`SKILL.md`](SKILL.md) (and [`.agents/skills/clipper-io/SKILL.md`](.agents/skills/clipper-io/SKILL.md)) compatible with **Antigravity, Claude Code, Cursor, Codex, Windsurf, Gemini CLI, and Agensi**.

## Quick Install & Run

### 1. Prerequisites
- **Python 3.10+**
- **ffmpeg** (`winget install Gyan.FFmpeg` on Windows, `brew install ffmpeg` on macOS, `sudo apt install ffmpeg` on Linux)
- **Deno** or **Node.js** (required by `yt-dlp` for YouTube JS extraction)
- *(Optional)* **Ollama** (`ollama pull qwen3.5:4b`) or a `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` in `.env`

### 2. Setup & Launch
```bash
git clone https://github.com/Umair-Zahid2510/clipper.git
cd clipper
```
- **Windows**: Double-click `setup.bat`, then `run.bat`
- **macOS / Linux**: `./setup.sh`, then `./run.sh`

Open **http://127.0.0.1:8765** (Main Clipper), **/watcher** (Live Watcher), **/campaigns** (Campaign Studio), or **/posts** (Social Queue & Auto-Post Loop).

## Configuration (`.env`)

Copy `.env.example` to `.env`:

| Setting | Purpose |
|---|---|
| `AI_PROVIDER` | `ollama` (local), `gemini`, `claude`, or `none` (fast deterministic clipper copy) |
| `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | Cloud LLM key if not using local Ollama |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | YouTube Data API v3 OAuth ([docs/youtube-setup.md](docs/youtube-setup.md)) |
| `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` | TikTok Content Posting API v2 PKCE OAuth |

## Known Platform Limits
- **YouTube Data API v3**: `videos.insert` has a 100 calls/day quota bucket (resets midnight PT). Enable **Auto-wait on quota reset** in `/posts` for multi-day queues.
- **TikTok Unaudited / Sandbox Apps**: Public accounts fall back to Inbox Drafts (`MEDIA_UPLOAD`), which TikTok caps at ~5 unpublished drafts until cleared in the TikTok mobile app (**Inbox -> System Notifications**).
