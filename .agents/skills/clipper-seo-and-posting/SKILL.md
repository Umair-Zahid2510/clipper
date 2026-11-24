---
name: clipper-seo-and-posting
description: >-
  Configure free daily-refreshing LLM APIs (Groq, Gemini Free, OpenRouter),
  fetch zero-cost YouTube/TikTok SEO hashtags, and handle YouTube/TikTok
  upload quota limits in clipper.io.
---

# Clipper SEO, Free Tokens & Posting Runbook

## 1. Free Refreshing LLM APIs (No Credit Card)
- **Groq (Recommended when local Ollama OOMs)**: OpenAI-compatible `https://api.groq.com/openai/v1` — `openai/gpt-oss-120b` or `qwen/qwen3.8-27b` gives **30 RPM / 1,000 RPD / 200K TPD**, plus `whisper-large-v3-turbo` (**2,000 RPD**), resetting daily.
- **Google Gemini Free Tier**: `gemini-3.5-flash-lite` / `gemini-3.6-flash` (`$0`, resets midnight PT). Set `GEMINI_API_KEY` in `.env` and `AI_PROVIDER=gemini`.
- **OpenRouter `:free`**: `https://openrouter.ai/api/v1` — **20 RPM / 50 RPD** free (`1,000 RPD` with `$10` one-time credit).

## 2. Free Real-Time SEO & Hashtag Sourcing
1. Query YouTube Autocomplete (zero auth/quota):
   `https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=<topic>`
2. Check Google Trends (`https://trends.google.com/trending?geo=US`) and TikTok Creative Center top 3 hashtags (`https://ads.tiktok.com/creative/creativeCenter/trends/hashtag?region=US&period=7`).
3. Combine: `campaign required_hashtags` + `creator tag` + `1–2 live autocomplete/niche tags` + `shorts` (max 5 tags).

## 3. YouTube & TikTok Upload Quota Handling
- **YouTube**: `videos.insert` has a **100 calls/day** bucket resetting midnight PT (no private-only lock). Enable `auto_wait_quota` + `±30%` jitter in `/posts`, or submit the [Quota Extension Form](https://support.google.com/youtube/contact/yt_api_form) for higher volume.
- **TikTok**: Unaudited apps fall back to `MEDIA_UPLOAD` (~5 pending Inbox drafts). Clear drafts in TikTok mobile app (`Inbox -> System Notifications`) and click **Resume**.
