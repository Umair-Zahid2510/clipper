# clip-watcher

Monitors a live Twitch stream in real time, detects chat hype spikes via anonymous IRC, and exports 30-second vertical (`9:16`) or landscape (`16:9`) captioned clips to `output_clips/`.

## Requirements

- Python 3.10+ (uses the parent Clipper `.venv`)
- `ffmpeg` and `ffprobe` on `PATH`
- Python packages in `requirements.txt` (`streamlink`, `faster-whisper`, `rich`, `python-dotenv`, `numpy`)

## Configuration

Edit `.env` (copied from `.env.example`):

```ini
TWITCH_CHANNEL=shroud
SPIKE_MULTIPLIER=2.5
MIN_SPIKE_MESSAGES=10
COOLDOWN_SECONDS=60
WHISPER_MODEL=tiny
VERTICAL_MODE=true
VERTICAL_LAYOUT=blur
SEGMENT_SECONDS=5
RETENTION_SECONDS=300
```

## Run

From the repository root (`clipper`):

```powershell
.\.venv\Scripts\python.exe clip-watcher\main.py
```

Or override the channel on the CLI:

```powershell
.\.venv\Scripts\python.exe clip-watcher\main.py --channel shroud
```

## Verify / Test

Run the end-to-end simulation test (chat spike detection, rolling segment cleanup, 30s FFmpeg cut + 9:16 layout, Whisper `.srt` generation, and caption burn-in):

```powershell
.\.venv\Scripts\python.exe clip-watcher\test_pipeline.py
```

