# Clipper Project Rules

- **ASS Hook Banners (`app/render.py`, `clip-watcher/captioner.py`)**: Never include emojis or non-ASCII characters in burned-in ASS `CliffBox`/`Hook` styles (`libass` renders broken glyphs). Keep white hook banners `<= 42` chars and write whole-reel curiosity hooks—never raw opening ASR words.
- **Campaign Compliance & Captions**: Always read active rules via `campaigns.get_campaign()` instead of hardcoded streamer defaults. Write titles/descriptions in 3rd-person clipper reaction style with a comment question (never raw transcript dumps).
- **Platform Rate-Limit & Policy Guardrails**:
  - Keep `>= 15s` jittered pacing between TikTok `init` calls, cache `force_inbox_draft` for unaudited public accounts, and pause cleanly on `spam_risk_too_many_pending_share`.
  - Never create multiple Google Cloud projects to shard YouTube Data API v3 quota for the same app (prohibited by YouTube Developer Policies). Use drip scheduling (`auto_wait_quota`) or the official Quota Extension Audit.

