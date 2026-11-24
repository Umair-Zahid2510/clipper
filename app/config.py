"""Paths and settings shared across the app."""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# override: the app's .env wins over machine-wide variables (e.g. an older OLLAMA_MODELS)
load_dotenv(ROOT / ".env", override=True)

DATA_DIR = Path(os.environ.get("CLIPSMITH_DATA_DIR") or ROOT / "data")
JOBS_DIR = DATA_DIR / "jobs"
MODELS_DIR = DATA_DIR / "models"
STATIC_DIR = ROOT / "static"

for _d in (JOBS_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Keep model downloads inside the project instead of the user profile.
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf"))

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "inclusionai/ling-3.2-flash:free")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

OUT_W, OUT_H = 1080, 1920
MAX_VIDEO_HOURS = 4


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")
# Explicit path from .env, else whatever "ollama" is on PATH (a normal install).
OLLAMA_EXE = os.environ.get("OLLAMA_EXE", "") or shutil.which("ollama") or ""
OLLAMA_MODELS_DIR = os.environ.get("OLLAMA_MODELS", "")
OLLAMA_CTX = int(os.environ.get("OLLAMA_CTX", "8192"))


def ai_providers() -> list[str]:
    """Ordered list of configured AI providers for automatic fallback (forced AI_PROVIDER first)."""
    forced = os.environ.get("AI_PROVIDER", "").strip().lower()
    if forced in ("none", "basic"):
        return []
    avail: list[str] = []
    if os.environ.get("GROQ_API_KEY"):
        avail.append("groq")
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        avail.append("gemini")
    if os.environ.get("OPENROUTER_API_KEY"):
        avail.append("openrouter")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        avail.append("claude")
    if OLLAMA_EXE and os.path.exists(OLLAMA_EXE):
        avail.append("ollama")
    if forced in ("ollama", "gemini", "claude", "groq", "openrouter"):
        return [forced] + [p for p in avail if p != forced]
    return avail


def ai_provider() -> str | None:
    """Primary AI provider (backward-compatible wrapper over ai_providers())."""
    provs = ai_providers()
    return provs[0] if provs else None


def missing_tools() -> list[str]:
    """Programs the pipeline shells out to."""
    return [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
