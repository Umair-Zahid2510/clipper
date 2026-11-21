"""Web server: accepts a YouTube link, runs the clipping pipeline in the
background, and serves progress and finished clips to the page."""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.background import BackgroundTask

from . import campaigns, config, render, selector, socials, source, trim, watcher, youtube

app = FastAPI(title="clipper.io")


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"} | {
    h.strip().lower() for h in os.environ.get("CLIPSMITH_ALLOWED_HOSTS", "").split(",") if h.strip()
}


def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8765
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Only this computer's own web pages may use the app.

    - Host check (DNS rebinding): a site can point its own name at 127.0.0.1,
      making its requests look same-origin. Requests not addressed to
      127.0.0.1 / localhost are refused, reads included.
    - Origin check (CSRF): browsers send Origin on cross-site POST/DELETE; a
      page on another site must not delete videos, post clips or disconnect
      YouTube. Extra hostnames can be allowed with CLIPSMITH_ALLOWED_HOSTS."""
    if _hostname(request.headers.get("host", "")) not in _LOCAL_HOSTS:
        return JSONResponse({"detail": "Open Clipsmith at http://127.0.0.1:8765."}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Request from another website refused."}, status_code=403)
    return await call_next(request)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=1)  # one video at a time; encoding uses every core
_upload_pool = ThreadPoolExecutor(max_workers=1)  # YouTube uploads don't wait behind rendering

_YT = re.compile(r"^https?://([\w-]+\.)?(youtube\.com|youtu\.be)/", re.I)


class JobRequest(BaseModel):
    url: str
    clip_count: int = Field(default=5, ge=1, le=15)
    min_len: int = Field(default=30, ge=10, le=170)
    max_len: int = Field(default=60, ge=15, le=180)
    style: str = "yellow"
    layout: str = "auto"
    hook: bool = True
    prompt: str = Field(default="", max_length=300)
    start_time: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end_time: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    aspect: str = "9:16"
    trim_pauses: bool = True
    auto_post: bool = False
    auto_privacy: str = "public"
    auto_account_id: str | None = None


_JOB_ID = re.compile(r"[0-9a-f]{10}")  # uuid4().hex[:10], see _new_job


def _job_dir(job_id: str) -> Path:
    """A job's folder. Job IDs come from URLs, so anything that isn't exactly a
    generated ID is refused before it can become part of a file path."""
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError("Not a valid job ID.")
    # Belt and braces: resolve the path and confirm it is still inside the jobs folder.
    root = os.path.realpath(config.JOBS_DIR)
    path = os.path.realpath(os.path.join(root, job_id))
    if not path.startswith(root + os.sep):
        raise ValueError("Not a valid job ID.")
    return Path(path)


def _save(job: dict) -> None:
    d = _job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "job.json")


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs[job_id]
        job.update(fields)
        _save(job)


def _load_existing() -> None:
    for f in config.JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _JOB_ID.fullmatch(str(job.get("id", ""))):
            continue  # not a job this app created
        if job.get("status") in ("queued", "running"):
            job["status"] = "error"
            job["error"] = "The app was closed while this was processing."
        for clip in job.get("clips") or []:
            for plat in ("youtube", "tiktok", "instagram"):
                pinfo = clip.get(plat)
                if pinfo and pinfo.get("status") in ("queued", "uploading"):
                    clip[plat] = {"status": "error", "error": "The app was closed during the upload. Post it again.", "retryable": True}
        _jobs[job["id"]] = job


_load_existing()


def _clock(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def friendly_error(exc: BaseException) -> str:
    """Turn a pipeline exception (often raw yt-dlp output) into a message for the page."""
    text = _ANSI.sub("", str(exc)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    low = text.lower()
    if "getaddrinfo" in low or "failed to resolve" in low or "connection refused" in low or "timed out" in low:
        return "Couldn't reach YouTube. Check your internet connection and try again."
    if "429" in low or "too many requests" in low:
        return "YouTube is limiting requests from your connection right now. Wait a few minutes and try again."
    text = re.sub(r"^\[\w+\]\s*[\w-]+:\s*", "", text)  # drop yt-dlp's "[youtube] <id>: " prefix
    return text or type(exc).__name__


def _render_bounds(
    clip_start: float,
    clip_end: float,
    words: list,
    duration: float | None,
    range_start: float | None = None,
    range_end: float | None = None,
) -> tuple[float, float]:
    """Pad a clip slightly so its first word isn't cut and the moment can land:
    run on into the pause after the last word (a laugh, a reaction, engine
    noise), never past the video or outside a requested From/To range."""
    start = max(0.0, clip_start - 0.15)
    next_word = next((w.start for w in words if w.start >= clip_end - 0.05), None)
    tail = 1.5 if next_word is None else max(0.3, min(1.5, next_word - clip_end - 0.1))
    end = clip_end + tail
    if duration:
        end = min(end, duration)
    if range_start is not None:
        start = max(start, range_start)
    if range_end is not None:
        end = min(end, range_end)
    return round(start, 3), round(end, 3)


def _run(job_id: str, req: JobRequest) -> None:
    workdir = _job_dir(job_id)

    last = {"pct": -1.0, "msg": ""}

    def progress(pct: float, message: str) -> None:
        pct = round(min(pct, 99), 1)
        # the download hook fires many times a second; don't rewrite job.json each time
        if message == last["msg"] and abs(pct - last["pct"]) < 1:
            return
        last.update(pct=pct, msg=message)
        _update(job_id, progress=pct, message=message)

    try:
        _update(job_id, status="running")
        upload = _jobs[job_id].get("upload")
        if upload:
            path = next(workdir.glob("source.*"))
            src = source.from_local_file(path, workdir, progress, title=Path(upload).stem)
        else:
            src = source.download(req.url, workdir, progress)
        _update(job_id, title=src.title, duration=src.duration, transcript_source=src.transcript_source)

        progress(42, "Finding the best moments")
        sentences = selector.within(selector.to_sentences(src.words), req.start_time, req.end_time)
        clips, method, warning = selector.select(
            sentences, src.title, req.clip_count, req.min_len, req.max_len, progress, prompt=req.prompt
        ) if sentences else ([], "basic", None)
        if not clips:
            ranged = req.start_time is not None or req.end_time is not None
            where = (
                f"between {_clock(req.start_time or 0)} and "
                f"{_clock(req.end_time) if req.end_time is not None else 'the end'} of this video"
                if ranged else "in this video"
            )
            raise RuntimeError(
                f"No stretch of speech between {req.min_len} and {req.max_len} seconds was found {where}."
            )
        _update(job_id, selection=method, warning=warning)

        done: list[dict] = []
        for i, clip in enumerate(clips):
            progress(45 + 54 * i / len(clips), f"Rendering clip {i + 1} of {len(clips)}")
            start, end = _render_bounds(
                clip.start, clip.end, src.words, src.duration, req.start_time, req.end_time
            )
            name = f"clip_{i + 1:02d}"
            keep = trim.plan(src.video_path, src.words, start, end) if req.trim_pauses else None
            info = render.render_clip(
                src.video_path, src.words, start, end, workdir, name,
                style=req.style, layout=req.layout, hook=clip.hook if req.hook else None, aspect=req.aspect,
                keep=keep,
            )
            done.append(
                asdict(clip)
                | {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "length": round(info.get("length", end - start), 1),
                    "video": f"/media/{job_id}/{name}.mp4",
                    "thumb": f"/media/{job_id}/{name}.jpg",
                    "layout": info["layout"],
                    "aspect": req.aspect,
                }
            )
            if req.auto_post and youtube.status()["connected"]:
                try:
                    src_url = req.url if _YT.match(req.url or "") else None
                    yt_meta = youtube.build_metadata(
                        title=clip.title or f"Clip {i + 1}",
                        description=clip.description or "",
                        hashtags=clip.hashtags or ["shorts"],
                        privacy=req.auto_privacy if req.auto_privacy in youtube.PRIVACY else "public",
                        made_for_kids=False,
                        source_url=src_url,
                    )
                    done[-1]["youtube"] = {"status": "queued", "progress": 0}
                    _update(job_id, clips=list(done))
                    if req.auto_account_id:
                        _upload_pool.submit(_publish, job_id, i, yt_meta, True, req.auto_account_id)
                    else:
                        _upload_pool.submit(_publish, job_id, i, yt_meta, True)
                except Exception:
                    _update(job_id, clips=list(done))
            else:
                _update(job_id, clips=list(done))

        src.video_path.unlink(missing_ok=True)  # the source is large; clips are what matter
        for leftover in workdir.glob("source.*"):
            leftover.unlink(missing_ok=True)
        _update(job_id, status="done", progress=100, message="Done")
    except Exception as exc:
        traceback.print_exc()
        _update(job_id, status="error", error=friendly_error(exc)[:500])


@app.get("/api/status")
def status() -> dict:
    return {
        "ai": config.ai_provider(),
        "missing": config.missing_tools(),
        "model": {
            "gemini": config.GEMINI_MODEL,
            "claude": config.CLAUDE_MODEL,
            "ollama": f"{config.OLLAMA_MODEL} (local)",
        }.get(config.ai_provider()),
        "styles": {k: v["label"] for k, v in render.STYLES.items()},
    }


def _check_options(req: JobRequest) -> None:
    if req.min_len >= req.max_len:
        raise HTTPException(400, "Minimum length must be shorter than maximum length.")
    if req.style not in render.STYLES:
        raise HTTPException(400, "Unknown caption style.")
    if req.layout not in ("auto", "fill", "fit", "split"):
        raise HTTPException(400, "Unknown layout.")
    if req.start_time is not None and req.end_time is not None and req.end_time <= req.start_time:
        raise HTTPException(400, "The 'To' time must be after the 'From' time.")
    if req.aspect not in render.ASPECTS:
        raise HTTPException(400, "Unknown aspect ratio.")


def _new_job(req: JobRequest, job_id: str | None = None, **extra) -> dict:
    job_id = job_id or uuid.uuid4().hex[:10]
    job = {
        "id": job_id, "url": req.url.strip(), "status": "queued", "progress": 0, "message": "Waiting to start",
        "title": None, "clips": [], "error": None, "warning": None, "selection": None,
        "created": time.time(), "options": req.model_dump(exclude={"url"}),
    } | extra
    with _lock:
        _jobs[job_id] = job
        _save(job)
    _pool.submit(_run, job_id, req)
    return job


@app.post("/api/jobs")
def create_job(req: JobRequest) -> dict:
    if not _YT.match(req.url.strip()):
        raise HTTPException(400, "That doesn't look like a YouTube link.")
    _check_options(req)
    return _new_job(req)


_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
_MAX_UPLOAD_BYTES = 8 * 1024**3


@app.post("/api/jobs/upload")
async def upload_job(
    file: UploadFile = File(...),
    clip_count: int = Form(5),
    min_len: int = Form(30),
    max_len: int = Form(60),
    style: str = Form("yellow"),
    layout: str = Form("auto"),
    hook: bool = Form(True),
    prompt: str = Form(""),
    start_time: float | None = Form(None),
    end_time: float | None = Form(None),
    aspect: str = Form("9:16"),
    trim_pauses: bool = Form(True),
    auto_post: bool = Form(False),
    auto_privacy: str = Form("public"),
    auto_account_id: str | None = Form(None),
) -> dict:
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise HTTPException(400, "Please upload a video file: mp4, mov, mkv or webm.")
    try:
        req = JobRequest(url="", clip_count=clip_count, min_len=min_len, max_len=max_len,
                         style=style, layout=layout, hook=hook,
                         prompt=prompt, start_time=start_time, end_time=end_time, aspect=aspect,
                         trim_pauses=trim_pauses, auto_post=auto_post, auto_privacy=auto_privacy,
                         auto_account_id=auto_account_id)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from None
    _check_options(req)
    job_id = uuid.uuid4().hex[:10]
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with open(d / f"source{ext}", "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File is too large (limit {_MAX_UPLOAD_BYTES // 1024**3} GB).")
                out.write(chunk)
    except BaseException:  # includes a cancelled/interrupted upload
        shutil.rmtree(d, ignore_errors=True)
        raise
    return _new_job(req, job_id, upload=name)


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str) -> FileResponse:
    with _lock:
        job = _jobs.get(job_id)
        # Copy the list: a clip can be deleted while the zip is being built.
        clips = list(job.get("clips") or []) if job else []
    if not job:
        raise HTTPException(404, "Job not found.")
    d = _job_dir(job_id)
    files = [d / Path(c["video"]).name for c in clips]
    files = [f for f in files if f.is_file()]
    if not files:
        raise HTTPException(409, "This video has no finished clips yet.")
    # Build in a throwaway file (outside the served /media tree) and delete it once sent,
    # so a download doesn't leave a second copy of every clip on disk.
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=config.DATA_DIR)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for f in files:
                try:
                    z.write(f, f.name)
                except FileNotFoundError:
                    continue  # deleted from another tab while zipping
    except BaseException:
        os.unlink(tmp)
        raise
    title = re.sub(r'[\\/:*?"<>|]+', "", job.get("title") or "clips").strip()[:80] or "clips"
    return FileResponse(tmp, media_type="application/zip", filename=f"{title}.zip",
                        background=BackgroundTask(os.unlink, tmp))


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    with _lock:
        return sorted(_jobs.values(), key=lambda j: -j["created"])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "This video is still processing.")
        if any((c.get("youtube") or {}).get("status") in ("queued", "uploading") for c in job.get("clips") or []):
            raise HTTPException(409, "A clip from this video is being posted to YouTube. Wait for it to finish.")
        del _jobs[job_id]
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}/clips/{index}")
def delete_clip(job_id: str, index: int) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        clips = job.get("clips") or []
        if not 0 <= index < len(clips):
            raise HTTPException(404, "Clip not found.")
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "This video is still processing.")
        # Uploads find their clip by position, so removing one would point them at the wrong clip.
        if any((c.get("youtube") or {}).get("status") in ("queued", "uploading") for c in clips):
            raise HTTPException(409, "A clip from this video is being posted to YouTube. Wait for it to finish.")
        clip = clips.pop(index)
        _save(job)
    d = _job_dir(job_id)
    for url in (clip.get("video"), clip.get("thumb")):
        if url:
            (d / Path(url).name).unlink(missing_ok=True)
    return {"ok": True}


# ---------- YouTube ----------

class PublishRequest(BaseModel):
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=10000)
    hashtags: list[str] = Field(default_factory=list, max_length=100)
    privacy: str = "public"
    publish_at: datetime | None = None
    made_for_kids: bool = False
    notify_subscribers: bool = True
    add_source_link: bool = False
    # The clip's video path when the dialog opened. Clips are addressed by position, so if one
    # was deleted meanwhile this catches a post landing on a different clip.
    clip_video: str | None = None
    account_id: str | None = None


class AccountSelectRequest(BaseModel):
    account_id: str


@app.get("/api/youtube")
def youtube_status() -> dict:
    return youtube.status()


@app.get("/api/youtube/accounts")
def youtube_accounts() -> dict:
    return youtube.list_accounts()


@app.post("/api/youtube/select")
def youtube_select_account(req: AccountSelectRequest) -> dict:
    try:
        return youtube.select_account(req.account_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@app.get("/api/youtube/connect")
def youtube_connect(request: Request, next: str = "/") -> RedirectResponse:
    # Google documents the desktop loopback redirect as http://127.0.0.1:port with
    # no path, so Google sends the user back to this app's root URL (see index()).
    redirect = str(request.base_url)
    try:
        url = youtube.start_auth(redirect)
    except youtube.NotConfigured as exc:
        raise HTTPException(400, str(exc)) from None
    if next in ("/", "/watcher", "/posts", "/campaigns"):
        youtube.set_return_to(url, next)
    return RedirectResponse(url, status_code=307)


@app.post("/api/youtube/disconnect")
def youtube_disconnect(account_id: str | None = None) -> dict:
    if account_id:
        youtube.disconnect(account_id)
    else:
        youtube.disconnect()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/clips/{index}/youtube")
def publish_clip(job_id: str, index: int, req: PublishRequest) -> dict:
    if not youtube.status()["connected"]:
        raise HTTPException(409, "Connect YouTube first.")
    with _lock:
        job = _jobs.get(job_id)
        clips = (job or {}).get("clips") or []
        if not job or not 0 <= index < len(clips):
            raise HTTPException(404, "Clip not found.")
        clip = clips[index]
        if req.clip_video is not None and req.clip_video != clip.get("video"):
            raise HTTPException(409, "This clip changed since you opened it (another clip was deleted). Reload the page and try again.")
        if (clip.get("youtube") or {}).get("status") in ("queued", "uploading"):
            raise HTTPException(409, "This clip is already being posted.")
        source_url = job.get("url") if req.add_source_link and _YT.match(job.get("url") or "") else None
        try:
            meta = youtube.build_metadata(
                title=req.title, description=req.description, hashtags=req.hashtags, privacy=req.privacy,
                publish_at=req.publish_at, made_for_kids=req.made_for_kids, source_url=source_url,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        clip["youtube"] = {"status": "queued", "progress": 0}
        _save(job)
    if req.account_id:
        _upload_pool.submit(_publish, job_id, index, meta, req.notify_subscribers, req.account_id)
    else:
        _upload_pool.submit(_publish, job_id, index, meta, req.notify_subscribers)
    return clip["youtube"]


def _set_youtube(job_id: str, index: int, value: dict) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job and index < len(job.get("clips") or []):
            job["clips"][index]["youtube"] = value
            _save(job)


def _publish(job_id: str, index: int, meta: dict, notify: bool, account_id: str | None = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        clip = job["clips"][index] if job else None
    if not clip:
        return
    path = _job_dir(job_id) / Path(clip["video"]).name
    last = {"pct": -10}

    def progress(pct: int) -> None:
        if pct - last["pct"] >= 5 or pct == 100:  # don't rewrite job.json for every chunk
            last["pct"] = pct
            _set_youtube(job_id, index, {"status": "uploading", "progress": pct})

    try:
        _set_youtube(job_id, index, {"status": "uploading", "progress": 0})
        if account_id:
            video_id = youtube.upload(path, meta, notify_subscribers=notify, progress=progress, account_id=account_id)
        else:
            video_id = youtube.upload(path, meta, notify_subscribers=notify, progress=progress)
        result = {"status": "done", "video_id": video_id, "url": f"https://youtu.be/{video_id}"}
        if meta.get("status", {}).get("publishAt"):
            result["scheduled_for"] = meta["status"]["publishAt"]
        else:
            result["privacy"] = meta.get("status", {}).get("privacyStatus")
        _set_youtube(job_id, index, result)
    except youtube.UploadError as exc:
        _set_youtube(job_id, index, {"status": "error", "error": str(exc)})
    except Exception as exc:
        traceback.print_exc()
        _set_youtube(job_id, index, {"status": "error", "error": f"Upload failed: {exc}"[:300]})


# ---------- Live stream watcher ----------

class WatcherStartRequest(BaseModel):
    url: str
    multiplier: float = Field(default=2.5, ge=1.2, le=10.0)
    min_messages: int = Field(default=10, ge=1, le=500)
    vertical_mode: bool = True
    vertical_layout: str = "blur"
    auto_post: bool | None = None
    auto_privacy: str | None = None
    auto_account_id: str | None = None
    campaign_mode: bool | None = None
    backfill_enabled: bool | None = None
    backfill_hours: float | None = Field(default=None, ge=0.25, le=12.0)


class WatcherBackfillRequest(BaseModel):
    url: str | None = None
    hours: float = Field(default=4.0, ge=0.25, le=12.0)


@app.get("/watcher")
def watcher_page():
    return FileResponse(config.STATIC_DIR / "watcher.html")


@app.get("/api/watcher/status")
def watcher_status() -> dict:
    return watcher.controller.status()


@app.get("/api/watcher/overlay")
def watcher_get_overlay() -> dict:
    return watcher.controller.overlay_status()


@app.post("/api/watcher/overlay")
async def watcher_upload_overlay(file: UploadFile = File(...)) -> dict:
    content = await file.read(50 * 1024 * 1024)
    try:
        return watcher.controller.save_overlay(file.filename or "logo_overlay.png", content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.delete("/api/watcher/overlay")
def watcher_delete_overlay() -> dict:
    return watcher.controller.remove_overlay()


@app.post("/api/watcher/start")
async def watcher_start(req: WatcherStartRequest) -> dict:
    try:
        return await watcher.controller.start(
            url=req.url,
            multiplier=req.multiplier,
            min_messages=req.min_messages,
            vertical_mode=req.vertical_mode,
            vertical_layout=req.vertical_layout,
            auto_post=req.auto_post,
            auto_privacy=req.auto_privacy,
            auto_account_id=req.auto_account_id,
            campaign_mode=req.campaign_mode,
            backfill_enabled=req.backfill_enabled,
            backfill_hours=req.backfill_hours,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/watcher/backfill")
async def watcher_backfill(req: WatcherBackfillRequest) -> dict:
    try:
        return await watcher.controller.start_backfill_now(url=req.url, hours=req.hours)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/watcher/stop")
async def watcher_stop() -> dict:
    return await watcher.controller.stop()


@app.post("/api/watcher/trigger")
async def watcher_trigger() -> dict:
    try:
        return await watcher.controller.trigger_clip_now()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None


@app.delete("/api/watcher/clips/{name}")
def watcher_delete_clip(name: str) -> dict:
    try:
        watcher.controller.delete_clip(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except FileNotFoundError:
        raise HTTPException(404, "Clip not found.") from None
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}


class ClearWatcherClipsRequest(BaseModel):
    keep_posted: bool = True
    channel: str | None = None


@app.post("/api/watcher/clips/clear")
def watcher_clear_clips(req: ClearWatcherClipsRequest) -> dict:
    return watcher.controller.clear_clips(keep_posted=req.keep_posted, channel=req.channel)


@app.post("/api/watcher/clips/{name}/ai-caption")
async def watcher_ai_caption(name: str) -> dict:
    try:
        return await asyncio.to_thread(watcher.controller.generate_ai_metadata, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except FileNotFoundError:
        raise HTTPException(404, "Clip not found.") from None


@app.post("/api/watcher/clips/{name}/youtube")
def publish_watcher_clip(name: str, req: PublishRequest) -> dict:
    if not youtube.status()["connected"]:
        raise HTTPException(409, "Connect YouTube first.")
    try:
        path = watcher.controller.resolve_clip_path(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except FileNotFoundError:
        raise HTTPException(404, "Clip not found.") from None

    clip_meta = watcher.controller.read_meta(path)
    if (clip_meta.get("youtube") or {}).get("status") in ("queued", "uploading"):
        raise HTTPException(409, "This clip is already being posted.")

    chan = clip_meta.get("channel") or watcher.controller.channel
    is_camp = watcher.controller.campaign_mode
    title_clean = watcher.captioner.strip_urls(req.title) if is_camp else req.title
    desc_clean = watcher.captioner.strip_urls(req.description) if is_camp else req.description
    source_url = None if is_camp else (f"https://www.twitch.tv/{chan}" if (req.add_source_link and chan) else None)
    try:
        meta = youtube.build_metadata(
            title=title_clean, description=desc_clean, hashtags=req.hashtags, privacy=req.privacy,
            publish_at=req.publish_at, made_for_kids=req.made_for_kids, source_url=source_url,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    queued_state = {"status": "queued", "progress": 0}
    watcher.controller.update_meta(
        name,
        title=title_clean.strip()[:100],
        description=desc_clean,
        hashtags=req.hashtags,
        youtube=queued_state,
    )
    _upload_pool.submit(_publish_watcher_clip, name, path, meta, req.notify_subscribers, req.account_id)
    return queued_state


def _publish_watcher_clip(
    name: str, path: Path, meta: dict, notify: bool, account_id: str | None = None
) -> None:
    last = {"pct": -10}

    def progress(pct: int) -> None:
        if pct - last["pct"] >= 5 or pct == 100:
            last["pct"] = pct
            watcher.controller.set_youtube(name, {"status": "uploading", "progress": pct})

    try:
        watcher.controller.set_youtube(name, {"status": "uploading", "progress": 0})
        if account_id:
            video_id = youtube.upload(path, meta, notify_subscribers=notify, progress=progress, account_id=account_id)
        else:
            video_id = youtube.upload(path, meta, notify_subscribers=notify, progress=progress)
        result = {"status": "done", "video_id": video_id, "url": f"https://youtu.be/{video_id}"}
        if meta.get("status", {}).get("publishAt"):
            result["scheduled_for"] = meta["status"]["publishAt"]
        else:
            result["privacy"] = meta.get("status", {}).get("privacyStatus")
        watcher.controller.set_youtube(name, result)
    except youtube.UploadError as exc:
        watcher.controller.set_youtube(name, {"status": "error", "error": str(exc)})
    except Exception as exc:
        traceback.print_exc()
        watcher.controller.set_youtube(name, {"status": "error", "error": f"Upload failed: {exc}"[:300]})


def _auto_publish_watcher_clip(name: str) -> None:
    if not youtube.status()["connected"]:
        return
    try:
        path = watcher.controller.resolve_clip_path(name)
        cmeta = watcher.controller.read_meta(path)
        if (cmeta.get("youtube") or {}).get("status") in ("queued", "uploading", "done"):
            return
        chan = cmeta.get("channel") or watcher.controller.channel
        is_camp = watcher.controller.campaign_mode
        src_url = None if is_camp else (f"https://www.twitch.tv/{chan}" if chan else None)
        priv = watcher.controller.auto_privacy if watcher.controller.auto_privacy in youtube.PRIVACY else "public"
        meta = youtube.build_metadata(
            title=cmeta.get("title") or Path(name).stem,
            description=cmeta.get("description") or "",
            hashtags=cmeta.get("hashtags") or list(watcher.cw_config.CAMPAIGN_HASHTAGS),
            privacy=priv,
            made_for_kids=False,
            source_url=src_url,
        )
        watcher.controller.set_youtube(name, {"status": "queued", "progress": 0})
        _upload_pool.submit(
            _publish_watcher_clip, name, path, meta, True, watcher.controller.auto_account_id
        )
    except Exception:
        pass


watcher.controller.on_clip_exported = _auto_publish_watcher_clip


# ---------- Posts, Multi-Social Setup, Approval Queue & Sequential Loop ----------

class AutoPostSettingsRequest(BaseModel):
    enabled: bool
    privacy: str = "public"
    account_id: str | None = None
    post_all_ready: bool = False


class YouTubeCredentialsRequest(BaseModel):
    client_id: str
    client_secret: str


class TikTokCredentialsRequest(BaseModel):
    client_key: str
    client_secret: str
    redirect_uri: str | None = None


class TikTokExchangeCodeRequest(BaseModel):
    code_or_url: str
    handle: str = "@clipcurrent1000"
    redirect_uri: str | None = None


class AddSocialAccountRequest(BaseModel):
    platform: str  # "tiktok" or "instagram"
    title: str
    access_token: str
    ig_user_id: str | None = None
    open_id: str | None = None
    refresh_token: str | None = None
    client_key: str | None = None
    client_secret: str | None = None


class SelectSocialAccountRequest(BaseModel):
    platform: str  # "youtube", "tiktok", "instagram"
    account_id: str


class ClipDetailsUpdateRequest(BaseModel):
    source: str  # "job" or "watcher"
    job_id: str | None = None
    index: int | None = None
    name: str | None = None
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=10000)
    hashtags: list[str] = Field(default_factory=list, max_length=100)
    privacy: str = "public"
    account_id: str | None = None
    add_source_link: bool = True
    made_for_kids: bool = False
    notify_subscribers: bool = True
    target_platforms: list[str] | None = None
    approval_status: str | None = None
    tiktok_options: dict | None = None
    instagram_options: dict | None = None


class ApproveRequest(BaseModel):
    key: str | None = None
    approval_status: str = "approved"  # "pending", "approved", "rejected"
    approve_all_pending: bool = False
    reset_all_to_pending: bool = False
    retry_failed: bool = False


class MultiPublishRequest(BaseModel):
    key: str
    platforms: list[str] = Field(default_factory=lambda: ["youtube"])


class SequentialLoopRequest(BaseModel):
    running: bool
    platforms: list[str] = Field(default_factory=lambda: ["youtube"])
    delay_seconds: int = Field(default=10, ge=0, le=86400)
    auto_approve_new: bool = False
    auto_retry_errors: bool = True


_loop_lock = threading.Lock()
_loop_state: dict = {
    "running": False,
    "platforms": ["youtube"],
    "delay_seconds": 10,
    "auto_approve_new": False,
    "auto_retry_errors": True,
    "current_key": None,
    "current_title": None,
    "current_platform": None,
    "posted_in_run": 0,
    "retried_in_run": 0,
    "last_error": None,
    "status_text": "Idle — approve clips below and start the loop",
    "next_post_at": None,
    "events": [],
}
_loop_thread: threading.Thread | None = None


def _log_loop_event(level: str, message: str, key: str | None = None, platform: str | None = None) -> None:
    """Append a timestamped event to the sequential loop log (kept to last 40 entries)."""
    entry = {
        "ts": time.time(),
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,  # "info", "ok", "warn", "err"
        "message": message,
        "key": key,
        "platform": platform,
    }
    with _loop_lock:
        ev = list(_loop_state.get("events") or [])
        ev.insert(0, entry)
        _loop_state["events"] = ev[:40]


@app.get("/posts")
def posts_page():
    return FileResponse(config.STATIC_DIR / "posts.html")


@app.get("/campaigns")
def campaigns_page():
    return FileResponse(config.STATIC_DIR / "campaigns.html")


class CampaignSelectRequest(BaseModel):
    campaign_id: str


class CampaignBriefParseRequest(BaseModel):
    brief: str


@app.get("/api/campaigns")
def get_campaigns_endpoint() -> dict:
    return campaigns.list_campaigns()


@app.post("/api/campaigns")
def save_campaign_endpoint(payload: dict) -> dict:
    try:
        res = campaigns.save_campaign(payload)
        watcher.controller.campaign_mode = res["active_id"] != "general"
        return res
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/campaigns/select")
def select_campaign_endpoint(req: CampaignSelectRequest) -> dict:
    try:
        res = campaigns.select_campaign(req.campaign_id)
        watcher.controller.campaign_mode = res["active_id"] != "general"
        return res
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/campaigns/parse-brief")
def parse_campaign_brief_endpoint(req: CampaignBriefParseRequest) -> dict:
    try:
        return campaigns.parse_rules_brief(req.brief)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/campaigns/{campaign_id}/overlay")
async def upload_campaign_overlay_endpoint(campaign_id: str, file: UploadFile = File(...)) -> dict:
    content = await file.read(50 * 1024 * 1024)
    try:
        return campaigns.save_campaign_overlay(campaign_id, file.filename or "logo_overlay.png", content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.delete("/api/campaigns/{campaign_id}")
def delete_campaign_endpoint(campaign_id: str) -> dict:
    try:
        return campaigns.delete_campaign(campaign_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/socials/accounts")
def get_all_social_accounts() -> dict:
    return socials.all_accounts_status()


@app.post("/api/socials/youtube-credentials")
def save_yt_credentials(req: YouTubeCredentialsRequest) -> dict:
    try:
        return socials.save_youtube_client_credentials(req.client_id, req.client_secret)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/socials/tiktok-credentials")
def save_tt_credentials(req: TikTokCredentialsRequest) -> dict:
    try:
        return socials.save_tiktok_client_credentials(req.client_key, req.client_secret, req.redirect_uri)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/tiktok/connect")
def tiktok_connect(
    request: Request,
    handle: str = "@clipcurrent1000",
    redirect_uri: str | None = None,
) -> RedirectResponse:
    try:
        url = socials.start_tiktok_auth(redirect_uri=redirect_uri, handle_hint=handle)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return RedirectResponse(url, status_code=307)


@app.post("/api/socials/tiktok/exchange")
def tiktok_exchange_redirect_url(req: TikTokExchangeCodeRequest) -> dict:
    try:
        return socials.finish_tiktok_auth(
            state=None,
            code_or_url=req.code_or_url,
            handle_override=req.handle,
            redirect_uri_override=req.redirect_uri,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/tiktok/callback")
def tiktok_oauth_callback(state: str | None = None, code: str | None = None, error: str | None = None):
    if error or not code:
        return RedirectResponse("/posts?tiktok=error", status_code=303)
    try:
        socials.finish_tiktok_auth(state or "", code)
    except Exception:
        return RedirectResponse("/posts?tiktok=error", status_code=303)
    return RedirectResponse("/posts?tiktok=connected", status_code=303)


@app.post("/api/socials/tiktok/refresh")
def refresh_tiktok_token_endpoint(account_id: str | None = None) -> dict:
    try:
        acc = socials.refresh_tiktok_account_token(account_id)
        return {"ok": True, "account_id": acc.get("id"), "title": acc.get("title")}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/socials/accounts")
def add_social_account_endpoint(req: AddSocialAccountRequest) -> dict:
    try:
        return socials.add_social_account(
            platform=req.platform,
            title=req.title,
            access_token=req.access_token,
            ig_user_id=req.ig_user_id,
            open_id=req.open_id,
            refresh_token=req.refresh_token,
            client_key=req.client_key,
            client_secret=req.client_secret,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/socials/select")
def select_social_account_endpoint(req: SelectSocialAccountRequest) -> dict:
    try:
        return socials.select_social_account(req.platform, req.account_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@app.delete("/api/socials/accounts/{platform}")
def delete_social_account_endpoint(platform: str, account_id: str | None = None) -> dict:
    try:
        return socials.remove_social_account(platform, account_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def _build_post_preview(
    title: str, description: str, hashtags: list[str], privacy: str, source_url: str | None
) -> dict:
    try:
        meta = youtube.build_metadata(
            title=title or "Untitled Short",
            description=description or "",
            hashtags=hashtags or ["shorts"],
            privacy=privacy if privacy in youtube.PRIVACY else "public",
            source_url=source_url,
        )
        snip = meta["snippet"]
        return {
            "final_title": snip["title"],
            "final_description": snip["description"],
            "final_tags": snip.get("tags") or [],
            "privacy": meta["status"]["privacyStatus"],
            "title_chars": len(snip["title"]),
            "description_bytes": len(snip["description"].encode("utf-8")),
        }
    except ValueError as exc:
        return {"error": str(exc)}


def _normalize_target_platforms(plats: list[str] | None) -> list[str]:
    cleaned = [p.strip().lower() for p in (plats or []) if p and p.strip().lower() in socials.PLATFORMS]
    return list(dict.fromkeys(cleaned)) or ["youtube"]


@app.get("/api/posts")
def list_posts_and_stats(refresh_stats: bool = False) -> dict:
    items: list[dict] = []
    video_ids: list[str] = []
    auto_approve = bool(_loop_state.get("auto_approve_new"))

    with _lock:
        jobs_list = sorted(_jobs.values(), key=lambda j: -j["created"])
        for j in jobs_list:
            j_url = j.get("url") or ""
            is_yt = bool(_YT.match(j_url))
            for idx, c in enumerate(j.get("clips") or []):
                yt_info = c.get("youtube")
                if isinstance(yt_info, dict) and yt_info.get("video_id"):
                    video_ids.append(yt_info["video_id"])
                priv = c.get("privacy") or (yt_info or {}).get("privacy") or watcher.controller.auto_privacy
                add_link = c.get("add_source_link", is_yt)
                src_url = j_url if (add_link and is_yt) else None
                title = c.get("title") or f"Clip {idx + 1}"
                desc = c.get("description") or ""
                tags = c.get("hashtags") or ["shorts"]
                app_st = c.get("approval_status") or ("approved" if auto_approve else "pending")
                t_plats = _normalize_target_platforms(c.get("target_platforms"))
                item_dict = {
                    "key": f"job:{j['id']}:{idx}",
                    "source": "job",
                    "source_label": j.get("title") or j.get("upload") or "YouTube Video",
                    "source_url": j_url if is_yt else None,
                    "job_id": j["id"],
                    "index": idx,
                    "name": f"clip_{idx + 1:02d}.mp4",
                    "video": c.get("video"),
                    "thumb": c.get("thumb"),
                    "length": c.get("length"),
                    "aspect": c.get("aspect", "9:16"),
                    "score": c.get("score"),
                    "hook": c.get("hook"),
                    "created": j.get("created", 0),
                    "title": title,
                    "description": desc,
                    "hashtags": tags,
                    "privacy": priv,
                    "account_id": c.get("account_id") or watcher.controller.auto_account_id,
                    "add_source_link": bool(add_link),
                    "made_for_kids": bool(c.get("made_for_kids", False)),
                    "notify_subscribers": bool(c.get("notify_subscribers", True)),
                    "approval_status": app_st if app_st in socials.APPROVAL_STATES else "pending",
                    "target_platforms": t_plats,
                    "tiktok_options": c.get("tiktok_options") or {},
                    "instagram_options": c.get("instagram_options") or {},
                    "youtube": yt_info,
                    "tiktok": c.get("tiktok"),
                    "instagram": c.get("instagram"),
                    "preview": _build_post_preview(title, desc, tags, priv, src_url),
                }
                item_dict["payloads"] = socials.build_platform_payloads(item_dict)
                items.append(item_dict)

    for wc in watcher.controller.list_clips():
        yt_info = wc.get("youtube")
        if isinstance(yt_info, dict) and yt_info.get("video_id"):
            video_ids.append(yt_info["video_id"])
        chan = wc.get("channel")
        c_name = wc.get("campaign_name")
        is_camp = watcher.controller.campaign_mode
        t_url = None if is_camp else (f"https://www.twitch.tv/{chan}" if chan else None)
        add_link = False if is_camp else wc.get("add_source_link", True)
        priv = wc.get("privacy") or (yt_info or {}).get("privacy") or watcher.controller.auto_privacy
        title = wc.get("title") or wc["name"]
        desc = wc.get("description") or ""
        tags = wc.get("hashtags") or list(watcher.cw_config.CAMPAIGN_HASHTAGS)
        app_st = wc.get("approval_status") or ("approved" if auto_approve else "pending")
        t_plats = _normalize_target_platforms(wc.get("target_platforms"))
        src_lbl = f"Live (#{chan})" if chan else (c_name or "Live Stream")
        item_dict = {
            "key": f"watcher:{wc['name']}",
            "source": "watcher",
            "source_label": src_lbl,
            "source_url": t_url,
            "name": wc["name"],
            "video": wc["video"],
            "srt": wc.get("srt"),
            "size_mb": wc.get("size_mb"),
            "aspect": "9:16",
            "created": wc.get("created", 0),
            "title": title,
            "description": desc,
            "hashtags": tags,
            "privacy": priv,
            "account_id": wc.get("account_id") or watcher.controller.auto_account_id,
            "add_source_link": bool(add_link),
            "made_for_kids": bool(wc.get("made_for_kids", False)),
            "notify_subscribers": bool(wc.get("notify_subscribers", True)),
            "approval_status": app_st if app_st in socials.APPROVAL_STATES else "pending",
            "target_platforms": t_plats,
            "tiktok_options": wc.get("tiktok_options") or {},
            "instagram_options": wc.get("instagram_options") or {},
            "compliance": wc.get("compliance"),
            "campaign_id": wc.get("campaign_id"),
            "campaign_name": wc.get("campaign_name"),
            "cliffhanger_text": wc.get("cliffhanger_text"),
            "youtube": yt_info,
            "tiktok": wc.get("tiktok"),
            "instagram": wc.get("instagram"),
            "preview": _build_post_preview(title, desc, tags, priv, t_url if add_link else None),
        }
        item_dict["payloads"] = socials.build_platform_payloads(item_dict)
        items.append(item_dict)

    items.sort(key=lambda x: -float(x.get("created") or 0))
    yt_stats = youtube.fetch_video_stats(video_ids, force=refresh_stats)

    ready_n = queued_n = uploading_n = posted_n = failed_n = 0
    pending_approval_n = approved_n = approved_posted_n = rejected_n = 0
    total_views = total_likes = total_comments = 0
    total_payout = 0.0
    active_camp = campaigns.get_campaign()
    camp_cpm = float(active_camp.get("cpm_usd") or 1.0)
    camp_max_pay = float(active_camp.get("max_payout_usd") or 300.0)
    camp_min_eng = float(active_camp.get("min_engagement_pct") or 0.8)

    # Assign sequential queue numbers only to approved clips that have NOT finished posting
    queue_pos = 0
    for it in items:
        ap = it.get("approval_status", "pending")
        y = it.get("youtube") or {}
        tt = it.get("tiktok") or {}
        ig = it.get("instagram") or {}
        statuses = [s.get("status") for s in (y, tt, ig) if isinstance(s, dict) and s.get("status")]

        t_plats = it.get("target_platforms") or ["youtube"]
        all_targets_done = all(((it.get(p) or {}).get("status") == "done") for p in t_plats)
        needs_post = any(
            ((it.get(p) or {}).get("status") not in ("done", "queued", "uploading"))
            for p in t_plats
        )

        if ap == "approved":
            approved_n += 1
            if all_targets_done:
                approved_posted_n += 1
        elif ap == "rejected":
            rejected_n += 1
        else:
            pending_approval_n += 1

        if ap == "approved" and needs_post:
            queue_pos += 1
            it["queue_position"] = queue_pos
        else:
            it["queue_position"] = None

        if "uploading" in statuses:
            uploading_n += 1
        elif "queued" in statuses:
            queued_n += 1
        elif "done" in statuses:
            posted_n += 1
        elif "error" in statuses:
            failed_n += 1
            ready_n += 1
        else:
            ready_n += 1

        vid = y.get("video_id")
        if y.get("status") == "done" and vid and vid in yt_stats:
            s = dict(yt_stats[vid])
            v = s.get("views", 0)
            lk = s.get("likes", 0)
            cm = s.get("comments", 0)
            eng = round(((lk + cm) / v) * 100, 2) if v > 0 else 0.0
            payout = round(min(camp_max_pay, (v / 1000.0) * camp_cpm), 2) if eng >= camp_min_eng else 0.0
            s["engagement_rate"] = eng
            s["meets_08_pct"] = eng >= camp_min_eng
            s["estimated_payout_usd"] = payout
            it["live_stats"] = s
            total_views += v
            total_likes += lk
            total_comments += cm
            total_payout += payout

    overall_eng = round(((total_likes + total_comments) / total_views) * 100, 2) if total_views > 0 else 0.0
    return {
        "campaigns": campaigns.list_campaigns(),
        "socials": socials.all_accounts_status(),
        "loop": dict(_loop_state),
        "auto_post": {
            "enabled": watcher.controller.auto_post,
            "privacy": watcher.controller.auto_privacy,
            "account_id": watcher.controller.auto_account_id,
        },
        "summary": {
            "total_clips": len(items),
            "ready_count": ready_n,
            "queued_count": queued_n,
            "uploading_count": uploading_n,
            "posted_count": posted_n,
            "failed_count": failed_n,
            "pending_approval_count": pending_approval_n,
            "approved_count": approved_n,
            "approved_posted_count": approved_posted_n,
            "approved_unposted_count": queue_pos,
            "rejected_count": rejected_n,
            "daily_quota_limit": 100,
            "total_views": total_views,
            "total_likes": total_likes,
            "total_comments": total_comments,
            "overall_engagement_rate": overall_eng,
            "meets_08_pct": overall_eng >= camp_min_eng,
            "estimated_payout_usd": round(total_payout, 2),
            "updated_at": time.time(),
        },
        "items": items,
    }


def _set_clip_field_by_key(key: str, **fields) -> bool:
    if key.startswith("job:"):
        parts = key.split(":")
        if len(parts) != 3:
            return False
        jid, idx_s = parts[1], parts[2]
        try:
            idx = int(idx_s)
        except ValueError:
            return False
        with _lock:
            job = _jobs.get(jid)
            clips = (job or {}).get("clips") or []
            if not job or not (0 <= idx < len(clips)):
                return False
            clips[idx].update(fields)
            _save(job)
            return True
    elif key.startswith("watcher:"):
        name = key.split(":", 1)[1]
        try:
            watcher.controller.update_meta(name, **fields)
            return True
        except (ValueError, FileNotFoundError):
            return False
    return False


def _resolve_clip_video_path(it: dict) -> Path:
    if it["source"] == "job":
        return _job_dir(it["job_id"]) / Path(it["video"]).name
    return watcher.controller.resolve_clip_path(it["name"])


@app.patch("/api/posts/clip")
def update_post_clip_details(req: ClipDetailsUpdateRequest) -> dict:
    if req.privacy not in youtube.PRIVACY:
        raise HTTPException(400, "Unknown privacy setting.")
    title = req.title.replace("<", "").replace(">", "").strip()[:100]
    if not title:
        raise HTTPException(400, "The title can't be empty.")
    tags = [t.lstrip("#").strip() for t in req.hashtags if t.strip()]

    extra: dict = {
        "made_for_kids": bool(req.made_for_kids),
        "notify_subscribers": bool(req.notify_subscribers),
    }
    if req.target_platforms is not None:
        extra["target_platforms"] = _normalize_target_platforms(req.target_platforms)
    if req.approval_status in socials.APPROVAL_STATES:
        extra["approval_status"] = req.approval_status
    if req.tiktok_options is not None:
        extra["tiktok_options"] = req.tiktok_options
    if req.instagram_options is not None:
        extra["instagram_options"] = req.instagram_options

    if req.source == "job":
        with _lock:
            job = _jobs.get(req.job_id or "")
            clips = (job or {}).get("clips") or []
            if not job or req.index is None or not (0 <= req.index < len(clips)):
                raise HTTPException(404, "Clip not found.")
            c = clips[req.index]
            c["title"] = title
            c["description"] = req.description
            c["hashtags"] = tags
            c["privacy"] = req.privacy
            c["account_id"] = req.account_id
            c["add_source_link"] = req.add_source_link
            c.update(extra)
            _save(job)
        return {"ok": True}
    elif req.source == "watcher":
        try:
            watcher.controller.update_meta(
                req.name or "",
                title=title,
                description=req.description,
                hashtags=tags,
                privacy=req.privacy,
                account_id=req.account_id,
                add_source_link=req.add_source_link,
                **extra,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(404, "Clip not found.") from None
        return {"ok": True}
    raise HTTPException(400, "Unknown clip source.")


@app.post("/api/posts/approve")
def approve_post_clips(req: ApproveRequest) -> dict:
    if req.approval_status not in socials.APPROVAL_STATES:
        raise HTTPException(400, "Invalid approval status.")
    updated = 0
    if req.reset_all_to_pending:
        snap = list_posts_and_stats(refresh_stats=False)
        for it in snap["items"]:
            # Only reset unposted clips back to pending; keep already-posted clips marked as posted
            t_plats = it.get("target_platforms") or ["youtube"]
            all_done = all(((it.get(p) or {}).get("status") == "done") for p in t_plats)
            if not all_done and it.get("approval_status") == "approved":
                if _set_clip_field_by_key(it["key"], approval_status="pending"):
                    updated += 1
        _log_loop_event("info", f"Cleared {updated} unposted clips from Approved Queue back to Pending.")
        return {"ok": True, "updated": updated}
    if req.retry_failed:
        snap = list_posts_and_stats(refresh_stats=False)
        for it in snap["items"]:
            cleared_any = False
            for p in socials.PLATFORMS:
                if (it.get(p) or {}).get("status") == "error":
                    _set_clip_field_by_key(it["key"], **{p: None, "approval_status": "approved"})
                    cleared_any = True
            if cleared_any:
                updated += 1
        _log_loop_event("info", f"Reset {updated} failed clip(s) for graceful retry.")
        return {"ok": True, "updated": updated}
    if req.approve_all_pending:
        snap = list_posts_and_stats(refresh_stats=False)
        for it in snap["items"]:
            t_plats = it.get("target_platforms") or ["youtube"]
            all_done = all(((it.get(p) or {}).get("status") == "done") for p in t_plats)
            if not all_done and it.get("approval_status") != "approved":
                if _set_clip_field_by_key(it["key"], approval_status="approved"):
                    updated += 1
        _log_loop_event("info", f"Approved {updated} pending clip(s) into the sequential queue.")
        return {"ok": True, "updated": updated}
    if not req.key:
        raise HTTPException(400, "Provide a clip key or set approve_all_pending=true.")
    if not _set_clip_field_by_key(req.key, approval_status=req.approval_status):
        raise HTTPException(404, "Clip not found.")
    return {"ok": True, "updated": 1, "key": req.key, "approval_status": req.approval_status}


def _publish_single_platform_sync(it: dict, platform: str) -> dict:
    """Synchronously upload a clip to a single platform (youtube, tiktok, or instagram)."""
    key = it["key"]
    path = _resolve_clip_video_path(it)
    payloads = it.get("payloads") or socials.build_platform_payloads(it)
    plat_payload = payloads[platform]

    def set_plat_state(val: dict) -> None:
        _set_clip_field_by_key(key, **{platform: val})

    last = {"pct": -10}

    def progress(pct: int) -> None:
        if pct - last["pct"] >= 5 or pct == 100:
            last["pct"] = pct
            set_plat_state({"status": "uploading", "progress": pct})

    set_plat_state({"status": "uploading", "progress": 0})
    try:
        if platform == "youtube":
            if not youtube.status()["connected"]:
                raise RuntimeError("YouTube account not connected. Open Social Accounts & Tokens Setup first.")
            meta = plat_payload["body"]
            acc_id = plat_payload.get("account_id") or it.get("account_id")
            notify = bool(plat_payload.get("notify_subscribers", True))
            if acc_id:
                vid = youtube.upload(path, meta, notify_subscribers=notify, progress=progress, account_id=acc_id)
            else:
                vid = youtube.upload(path, meta, notify_subscribers=notify, progress=progress)
            res = {
                "status": "done",
                "video_id": vid,
                "url": f"https://youtu.be/{vid}",
                "privacy": meta.get("status", {}).get("privacyStatus", "public"),
            }
            set_plat_state(res)
            return res
        elif platform == "tiktok":
            acc_id = (it.get("tiktok_options") or {}).get("account_id")
            res = socials.upload_to_tiktok(path, plat_payload, account_id=acc_id, progress=progress)
            set_plat_state(res)
            return res
        elif platform == "instagram":
            acc_id = (it.get("instagram_options") or {}).get("account_id")
            res = socials.upload_to_instagram(path, plat_payload, account_id=acc_id, progress=progress)
            set_plat_state(res)
            return res
        else:
            raise ValueError(f"Unsupported platform: {platform}")
    except Exception as exc:
        err = {"status": "error", "error": str(exc)[:300]}
        set_plat_state(err)
        return err


@app.post("/api/posts/publish")
def publish_clip_multi_social(req: MultiPublishRequest) -> dict:
    platforms = _normalize_target_platforms(req.platforms)
    snap = list_posts_and_stats(refresh_stats=False)
    it = next((x for x in snap["items"] if x["key"] == req.key), None)
    if not it:
        raise HTTPException(404, "Clip not found.")

    acc_status = socials.all_accounts_status()
    for p in platforms:
        if not acc_status.get(p, {}).get("connected"):
            label = {"youtube": "YouTube Shorts", "tiktok": "TikTok", "instagram": "Instagram Reels"}.get(p, p)
            raise HTTPException(409, f"Connect {label} in Social Accounts & Tokens Setup first.")

    _set_clip_field_by_key(req.key, target_platforms=platforms)
    queued: dict[str, dict] = {}
    for p in platforms:
        cur = (it.get(p) or {}).get("status")
        if cur in ("queued", "uploading"):
            continue
        state = {"status": "queued", "progress": 0}
        _set_clip_field_by_key(req.key, **{p: state})
        queued[p] = state
        _upload_pool.submit(_publish_single_platform_sync, it, p)

    return {"ok": True, "key": req.key, "platforms": platforms, "queued": queued}


def _classify_upload_error(err_msg: str) -> str:
    """Classify an upload error into 'quota_or_auth', 'transient', or 'clip_invalid'."""
    low = (err_msg or "").lower()
    if any(k in low for k in ("429", "quota", "daily limit", "100 uploads", "upload limit")):
        return "quota"
    if any(k in low for k in ("401", "expired", "revoked", "not connected", "reconnect", "access_token")):
        return "auth"
    if any(k in low for k in ("rejected the title", "rejected the description", "rejected the hashtags", "invalid")):
        return "clip_invalid"
    return "transient"


def _sequential_loop_worker() -> None:
    """Background worker that posts approved clips one by one in sequence across target socials
    with graceful error classification, automatic exponential backoff retry, and account rotation."""
    while True:
        with _loop_lock:
            if not _loop_state["running"]:
                _loop_state["current_key"] = None
                _loop_state["current_title"] = None
                _loop_state["current_platform"] = None
                _loop_state["next_post_at"] = None
                _loop_state["status_text"] = "Stopped"
                return
            loop_plats = list(_loop_state["platforms"])
            delay_s = int(_loop_state["delay_seconds"])
            auto_retry = bool(_loop_state.get("auto_retry_errors", True))

        snap = list_posts_and_stats(refresh_stats=False)
        acc_status = snap.get("socials") or {}

        # Find the next approved clip that hasn't completed posting on all target platforms
        candidate = None
        candidate_plats: list[str] = []
        now = time.time()
        for it in snap["items"]:
            if it.get("approval_status") != "approved":
                continue
            c_plats = _normalize_target_platforms(it.get("target_platforms") or loop_plats)
            active_plats = [p for p in loop_plats if p in c_plats] or loop_plats
            pending_plats = []
            for p in active_plats:
                if not acc_status.get(p, {}).get("connected"):
                    continue
                pinfo = it.get(p) or {}
                pst = pinfo.get("status")
                if pst in ("done", "queued", "uploading"):
                    continue
                if pst == "error":
                    if not auto_retry:
                        continue
                    # Respect per-clip retry cooldown & max attempts (3) for transient errors
                    attempts = int(pinfo.get("attempts") or 1)
                    next_retry_at = float(pinfo.get("next_retry_at") or 0)
                    err_kind = pinfo.get("error_kind") or _classify_upload_error(pinfo.get("error") or "")
                    if err_kind in ("quota", "auth", "clip_invalid") or attempts >= 3 or now < next_retry_at:
                        continue
                pending_plats.append(p)
            if pending_plats:
                candidate = it
                candidate_plats = pending_plats
                break

        if not candidate:
            with _loop_lock:
                if not _loop_state["running"]:
                    return
                _loop_state["current_key"] = None
                _loop_state["current_title"] = None
                _loop_state["current_platform"] = None
                _loop_state["next_post_at"] = None
                _loop_state["status_text"] = "Monitoring queue — waiting for newly approved or retry-ready clips..."
            time.sleep(2.0)
            continue

        paused_due_to_fatal = False
        for p in candidate_plats:
            with _loop_lock:
                if not _loop_state["running"]:
                    return
                _loop_state["current_key"] = candidate["key"]
                _loop_state["current_title"] = candidate["title"]
                _loop_state["current_platform"] = p
                _loop_state["next_post_at"] = None
                _loop_state["status_text"] = f"Posting '{candidate['title'][:38]}' to {p.upper()}..."

            prev_info = candidate.get(p) or {}
            prev_attempts = int(prev_info.get("attempts") or 0)
            res = _publish_single_platform_sync(candidate, p)

            if res.get("status") == "done":
                with _loop_lock:
                    _loop_state["posted_in_run"] = int(_loop_state.get("posted_in_run", 0)) + 1
                    _loop_state["last_error"] = None
                    _loop_state["status_text"] = f"Posted '{candidate['title'][:38]}' to {p.upper()}"
                _log_loop_event("ok", f"Posted '{candidate['title'][:45]}' to {p.upper()} ({res.get('url') or 'done'})", candidate["key"], p)
            else:
                err_msg = res.get("error") or "Upload failed"
                err_kind = _classify_upload_error(err_msg)
                attempts = prev_attempts + 1

                # If YouTube hit daily quota and another YouTube channel is linked, rotate to it automatically!
                rotated_to = None
                if err_kind == "quota" and p == "youtube":
                    yt_accs = (acc_status.get("youtube") or {}).get("accounts") or []
                    cur_id = (acc_status.get("youtube") or {}).get("active_id")
                    alt = next((a for a in yt_accs if a.get("id") != cur_id), None)
                    if alt:
                        try:
                            youtube.select_account(alt["id"])
                            rotated_to = alt.get("title") or alt["id"]
                            _set_clip_field_by_key(candidate["key"], account_id=alt["id"], youtube=None)
                            _log_loop_event("warn", f"YouTube quota hit on current channel — auto-switched to '{rotated_to}' and queued clip for immediate resume.", candidate["key"], p)
                        except Exception:
                            rotated_to = None

                if rotated_to:
                    continue

                backoff_s = min(60, 5 * (2 ** (attempts - 1)))
                enriched_err = {
                    "status": "error",
                    "error": err_msg,
                    "error_kind": err_kind,
                    "attempts": attempts,
                    "next_retry_at": time.time() + backoff_s if err_kind == "transient" and attempts < 3 else None,
                    "retryable": err_kind != "clip_invalid",
                }
                _set_clip_field_by_key(candidate["key"], **{p: enriched_err})

                if err_kind in ("quota", "auth"):
                    # Check if any other selected platform in loop_plats is still connected and usable
                    other_connected = [x for x in loop_plats if x != p and acc_status.get(x, {}).get("connected")]
                    if not other_connected:
                        with _loop_lock:
                            _loop_state["running"] = False
                            _loop_state["current_key"] = None
                            _loop_state["current_platform"] = None
                            _loop_state["last_error"] = err_msg
                            _loop_state["status_text"] = f"Paused on {p.upper()} ({err_kind}): {err_msg[:90]} — click Resume when ready"
                        _log_loop_event("err", f"Loop auto-paused ({p.upper()} {err_kind}): {err_msg}. Add/switch channel or wait for quota reset, then click Resume.", candidate["key"], p)
                        paused_due_to_fatal = True
                        break
                    else:
                        _log_loop_event("warn", f"{p.upper()} paused ({err_kind}): {err_msg[:80]}. Continuing loop on {', '.join(other_connected)}.", candidate["key"], p)
                elif err_kind == "transient" and attempts < 3:
                    with _loop_lock:
                        _loop_state["retried_in_run"] = int(_loop_state.get("retried_in_run", 0)) + 1
                        _loop_state["last_error"] = err_msg
                        _loop_state["status_text"] = f"Transient error on {p.upper()} (attempt {attempts}/3) — auto-retrying in {backoff_s}s..."
                    _log_loop_event("warn", f"Transient failure on '{candidate['title'][:38]}' ({p.upper()}, attempt {attempts}/3): {err_msg[:80]} — scheduled auto-retry in {backoff_s}s.", candidate["key"], p)
                else:
                    with _loop_lock:
                        _loop_state["last_error"] = err_msg
                        _loop_state["status_text"] = f"Skipped '{candidate['title'][:32]}' on {p.upper()} after error: {err_msg[:70]}"
                    _log_loop_event("err", f"Failed '{candidate['title'][:40]}' on {p.upper()}: {err_msg}", candidate["key"], p)

        if paused_due_to_fatal:
            return

        # Pacing delay before the next clip in sequence
        if delay_s > 0:
            end_ts = time.time() + delay_s
            with _loop_lock:
                _loop_state["current_key"] = None
                _loop_state["current_platform"] = None
                _loop_state["next_post_at"] = end_ts
            while time.time() < end_ts:
                with _loop_lock:
                    if not _loop_state["running"]:
                        return
                    rem = max(0, int(end_ts - time.time()))
                    _loop_state["status_text"] = f"Waiting {rem}s before next approved clip in sequence..."
                time.sleep(1.0)


@app.post("/api/posts/loop")
def configure_sequential_loop(req: SequentialLoopRequest) -> dict:
    global _loop_thread
    plats = _normalize_target_platforms(req.platforms)
    if req.running:
        acc_status = socials.all_accounts_status()
        connected_any = any(acc_status.get(p, {}).get("connected") for p in plats)
        if not connected_any:
            labels = ", ".join({"youtube": "YouTube Shorts", "tiktok": "TikTok", "instagram": "Instagram Reels"}[p] for p in plats)
            raise HTTPException(409, f"Connect at least one selected platform ({labels}) in Social Accounts & Tokens Setup before starting the sequential loop.")

        # On start/resume, automatically reset retryable error states on approved clips so they gracefully resume
        snap = list_posts_and_stats(refresh_stats=False)
        resumed_errors = 0
        for it in snap["items"]:
            if it.get("approval_status") != "approved":
                continue
            for p in plats:
                pinfo = it.get(p) or {}
                if pinfo.get("status") == "error":
                    _set_clip_field_by_key(it["key"], **{p: None})
                    resumed_errors += 1
        if resumed_errors:
            _log_loop_event("info", f"Gracefully reset {resumed_errors} previously failed upload(s) to resume from queue.")

    with _loop_lock:
        was_running = _loop_state["running"]
        _loop_state["running"] = bool(req.running)
        _loop_state["platforms"] = plats
        _loop_state["delay_seconds"] = int(req.delay_seconds)
        _loop_state["auto_approve_new"] = bool(req.auto_approve_new)
        _loop_state["auto_retry_errors"] = bool(req.auto_retry_errors)
        if req.running and not was_running:
            _loop_state["posted_in_run"] = 0
            _loop_state["last_error"] = None
            _loop_state["status_text"] = "Starting sequential auto-post loop..."
            _loop_thread = threading.Thread(target=_sequential_loop_worker, daemon=True)
            _loop_thread.start()
        elif not req.running:
            _loop_state["current_key"] = None
            _loop_state["current_title"] = None
            _loop_state["current_platform"] = None
            _loop_state["next_post_at"] = None
            _loop_state["status_text"] = "Stopped"
        res_state = dict(_loop_state)
    if req.running and not was_running:
        _log_loop_event("info", f"Sequential loop started for {', '.join(p.upper() for p in plats)} (delay {req.delay_seconds}s).")
    elif not req.running and was_running:
        _log_loop_event("info", "Sequential loop paused by user.")
    return res_state


@app.post("/api/posts/auto")
def configure_auto_post(req: AutoPostSettingsRequest) -> dict:
    if req.privacy not in youtube.PRIVACY:
        raise HTTPException(400, "Unknown privacy setting.")
    watcher.controller.auto_post = bool(req.enabled)
    watcher.controller.auto_privacy = req.privacy
    watcher.controller.auto_account_id = req.account_id or None

    queued_now = 0
    if req.post_all_ready:
        if not youtube.status()["connected"]:
            raise HTTPException(409, "Connect YouTube first.")
        snapshot = list_posts_and_stats(refresh_stats=False)
        for it in snapshot["items"]:
            st = (it.get("youtube") or {}).get("status")
            if st in ("queued", "uploading", "done"):
                continue
            pub_req = PublishRequest(
                title=it["title"],
                description=it["description"],
                hashtags=it["hashtags"],
                privacy=it.get("privacy") or req.privacy,
                made_for_kids=bool(it.get("made_for_kids", False)),
                notify_subscribers=bool(it.get("notify_subscribers", True)),
                add_source_link=it.get("add_source_link", True),
                account_id=it.get("account_id") or req.account_id,
            )
            try:
                if it["source"] == "job":
                    publish_clip(it["job_id"], it["index"], pub_req)
                else:
                    publish_watcher_clip(it["name"], pub_req)
                queued_now += 1
            except HTTPException:
                continue

    return {
        "ok": True,
        "queued_now": queued_now,
        "auto_post": {
            "enabled": watcher.controller.auto_post,
            "privacy": watcher.controller.auto_privacy,
            "account_id": watcher.controller.auto_account_id,
        },
    }


atexit.register(watcher.controller.stop_sync)


app.mount("/media", StaticFiles(directory=config.JOBS_DIR), name="media")
app.mount("/watcher-media", StaticFiles(directory=watcher.OUTPUT_DIR), name="watcher-media")
app.mount("/watcher-assets", StaticFiles(directory=watcher.ASSETS_DIR), name="watcher-assets")


@app.get("/")
def index(state: str | None = None, code: str | None = None, error: str | None = None):
    # Google sends the browser back here after the YouTube sign-in.
    dest = youtube.get_return_to(state)
    if error and state:
        reason = "access_denied" if error == "access_denied" else "failed"  # never echo the raw value
        return RedirectResponse(f"{dest}?youtube=error&reason={reason}", status_code=303)
    if state and code:
        try:
            youtube.finish_auth(state, code)
        except youtube.AuthError:
            return RedirectResponse(f"{dest}?youtube=error&reason=expired", status_code=303)
        return RedirectResponse(f"{dest}?youtube=connected", status_code=303)
    return FileResponse(config.STATIC_DIR / "index.html")
