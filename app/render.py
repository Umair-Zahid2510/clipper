"""Turn a time range of the source video into a finished vertical clip:
reframe to 9:16 around the speaker, burn in animated captions, encode."""
from __future__ import annotations

import re
import statistics
import subprocess
from pathlib import Path

import cv2
import numpy as np

from . import config
from .source import Word

W, H = config.OUT_W, config.OUT_H

# Output sizes per aspect ratio; 9:16 is the default (Shorts / TikTok / Reels).
ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
DEFAULT_ASPECT = "9:16"

# ASS colours are &HBBGGRR&
STYLES = {
    "bw": {"label": "Mono White (B&W)", "font": "Arial Black", "size": 92, "active": "&HFFFFFF&", "upper": True},
    "yellow": {"label": "Bold Yellow", "font": "Arial Black", "size": 92, "active": "&H00F6FF&", "upper": True},
    "green": {"label": "Green Pop", "font": "Arial Black", "size": 92, "active": "&H5CFF4D&", "upper": True},
    "clean": {"label": "Clean White", "font": "Arial", "size": 84, "active": "&HFFD27A&", "upper": False},
}


# ---------- reframing ----------

Box = tuple[float, float, float, float]  # centre x, centre y, width, height; all 0..1 of the frame


def detect_faces(video: Path, start: float, end: float, fps: float = 3.0) -> list[tuple[float, list[Box]]]:
    """Sample the clip and return (time_in_clip, every face box found in that frame)."""
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    src_w, src_h = _probe_size(video)
    fw_px = 480
    fh_px = int(src_h * fw_px / src_w) // 2 * 2
    # One ffmpeg pass piping small grey frames is far faster than seeking per sample.
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(video),
         "-an", "-vf", f"fps={fps},scale={fw_px}:{fh_px}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    samples: list[tuple[float, list[Box]]] = []
    frame_bytes = fw_px * fh_px
    i = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            img = np.frombuffer(buf, dtype=np.uint8).reshape(fh_px, fw_px)
            faces = cascade.detectMultiScale(img, scaleFactor=1.15, minNeighbors=6, minSize=(24, 24))
            samples.append((i / fps, [
                ((x + fw / 2) / fw_px, (y + fh / 2) / fh_px, fw / fw_px, fh / fh_px) for x, y, fw, fh in faces
            ]))
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()
    return samples


def _faces(entry) -> list[Box]:
    """A frame's faces as a list, whether given as a list, a single box or None."""
    if not entry:
        return []
    return [entry] if isinstance(entry[0], (int, float)) else list(entry)


def track_samples(boxes: list[tuple[float, list[Box] | Box | None]]) -> list[tuple[float, float | None]]:
    """Face boxes -> centre-x of the largest face per frame, for camera_path.
    A face under ~7% of frame width is a wide shot (stage, audience);
    following it gives a jumpy crop."""
    out = []
    for t, entry in boxes:
        big = [b for b in _faces(entry) if b[2] >= 0.07]
        out.append((t, max(big, key=lambda b: b[2] * b[3])[0] if big else None))
    return out


def track_faces(video: Path, start: float, end: float, fps: float = 3.0) -> list[tuple[float, float | None]]:
    """Sample the clip and return (time_in_clip, face_centre_x as 0..1 or None)."""
    return track_samples(detect_faces(video, start, end, fps))


def facecam_from_faces(boxes: list[tuple[float, list[Box] | Box | None]]) -> Box | None:
    """A streamer's facecam: a small face that stays put away from the middle of
    the frame. Every face in every frame is a candidate, so a bigger face in the
    content (a game character, a video being reacted to) can't hide it. People
    look away from the camera a lot, so a face in a quarter of the frames is
    enough. Returns the median box, or None for anything else (talking heads,
    moving people, rare detections)."""
    # small, off-centre faces only; a big or centred face is the main subject
    cands = [(i, b) for i, (_, entry) in enumerate(boxes) for b in _faces(entry)
             if b[2] <= 0.22 and not 0.3 <= b[0] <= 0.7]
    if not cands:
        return None

    def near(a: Box, b: Box, r: float) -> bool:
        return abs(a[0] - b[0]) <= r and abs(a[1] - b[1]) <= r

    # the spot where a face shows up most often...
    anchor = max((b for _, b in cands), key=lambda a: len({i for i, b in cands if near(a, b, 0.06)}))
    # ...and the one face per frame that sits there
    spot: dict[int, Box] = {}
    for i, b in cands:
        if near(anchor, b, 0.08) and i not in spot:
            spot[i] = b
    seen = list(spot.values())
    if len(seen) < max(3, 0.25 * len(boxes)):
        return None
    if statistics.pstdev([b[0] for b in seen]) > 0.06 or statistics.pstdev([b[1] for b in seen]) > 0.06:
        return None  # it moves around: a person in the scene, not a fixed camera overlay
    return tuple(statistics.median(b[k] for b in seen) for k in range(4))


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def split_layout(
    src_w: int, src_h: int, cam: Box, out_w: int, out_h: int, cam_share: float = 0.4
) -> dict | None:
    """Crops for a stacked frame: facecam on top (cam_share of the height),
    gameplay below. Each crop has its panel's shape, so nothing is stretched.
    None when no gameplay crop can leave the facecam out (it would show twice)."""
    top_h = _even(out_h * cam_share)
    bottom_h = out_h - top_h
    cx, cy, fw, fh = cam

    # Facecam: about 2.2 face-heights tall, a little below the face to show
    # the shoulders, shaped like the top panel.
    ch = min(src_h, fh * src_h * 2.2)
    cw = ch * out_w / top_h
    if cw > src_w:
        cw, ch = src_w, src_w * top_h / out_w
    cam_x = min(max(0, cx * src_w - cw / 2), src_w - cw)
    cam_y = min(max(0, cy * src_h - ch * 0.4), src_h - ch)

    # Gameplay: full height, from the middle of the frame, pushed past the
    # facecam (with a margin) when it would otherwise show it a second time.
    gh = src_h
    gw = gh * out_w / bottom_h
    if gw > src_w:
        gw, gh = src_w, src_w * bottom_h / out_w
    gx = (src_w - gw) / 2
    margin = 1.2 * fw * src_w
    face_left, face_right = cx * src_w - margin, cx * src_w + margin
    if cx < 0.5 and gx < face_right:
        gx = min(face_right, src_w - gw)
    elif cx >= 0.5 and gx + gw > face_left:
        gx = max(0, face_left - gw)
    gy = (src_h - gh) / 2
    if gx < (cx + fw / 2) * src_w and gx + gw > (cx - fw / 2) * src_w:
        return None  # the face itself would still be in the gameplay panel

    def rect(x, y, w, h):  # even sizes and offsets (yuv420p), kept inside the frame
        w, h = _even(min(w, src_w)), _even(min(h, src_h))
        x = int(min(max(0, x), src_w - w)) // 2 * 2
        y = int(min(max(0, y), src_h - h)) // 2 * 2
        return (x, y, w, h)

    return {"cam": rect(cam_x, cam_y, cw, ch), "game": rect(gx, gy, gw, gh), "top_h": top_h, "bottom_h": bottom_h}


def camera_path(samples: list[tuple[float, float | None]], deadzone: float = 0.09) -> list[tuple[float, float]] | None:
    """Convert face samples into a steady camera: hold position, and only move
    when the face has clearly left the current framing. Returns step keyframes
    (time, centre_x), or None when faces are too rare to trust."""
    seen = [x for _, x in samples if x is not None]
    if len(seen) < max(3, 0.5 * len(samples)):  # mostly wide shots: use the blurred "fit" layout instead
        return None

    cam = statistics.median(seen[: max(3, len(seen) // 10)])
    keys = [(0.0, cam)]
    pending: list[float] = []
    for t, x in samples:
        if x is None:
            continue
        if abs(x - cam) > deadzone:
            pending.append(x)
            if len(pending) >= 3:  # a full second off-centre: a real move, not a flicker
                cam = statistics.median(pending)
                keys.append((max(0.0, t - 0.6), float(cam)))
                pending = []
        else:
            pending = []
    return keys[:60]


def crop_width(src_w: int, src_h: int, out_w: int, out_h: int) -> int:
    """Width of the full-height source crop that matches the output ratio,
    rounded to even and clamped to the source width."""
    return min(int(src_h * out_w / out_h) // 2 * 2, src_w)


def _crop_x_expr(keys: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    def px(cx: float) -> int:
        return int(max(0, min(src_w - crop_w, cx * src_w - crop_w / 2)))

    expr = str(px(keys[-1][1]))
    for i in range(len(keys) - 2, -1, -1):
        expr = f"if(lt(t,{keys[i + 1][0]:.2f}),{px(keys[i][1])},{expr})"
    return expr


# ---------- captions ----------

def _ass_time(t: float) -> str:
    t = max(t, 0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def _groups(words: list[Word], max_words: int = 3, max_chars: int = 18) -> list[list[Word]]:
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        chars = sum(len(x.text) for x in cur) + len(cur)
        gap = (w.start - cur[-1].end) if cur else 0
        if cur and (len(cur) >= max_words or chars + len(w.text) > max_chars or gap > 0.6):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w.text[-1:] in ".?!,":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(
    words: list[Word], clip_start: float, clip_len: float, style_key: str, hook: str | None,
    size: tuple[int, int] = (W, H),
    hook_top: int | None = None,
) -> str:
    """`hook_top`: y of a panel seam to centre the hook banner on (split layout),
    instead of its usual place near the top of the frame."""
    st = STYLES.get(style_key, STYLES["yellow"])
    w, h = size
    # Margins are proportional to the frame (they reproduce the 9:16 values exactly);
    # in square/landscape frames the text is a little smaller so lines still fit.
    k = min(w, h) / 1080 * (1.0 if h > w else 0.85)
    cap_size, hook_size = round(st["size"] * k), round(52 * k)
    cap_lr, cap_v = round(w * 0.065), round(h * 0.245)
    hook_lr, hook_v = round(w * 0.09), round(h * 0.11)
    if hook_top is not None:
        hook_v = max(0, hook_top - round(hook_size * 0.6))  # straddle the seam, clear of the face
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,{cap_lr},{cap_lr},{cap_v},1
Style: Hook,Arial Black,{hook_size},&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,14,0,8,{hook_lr},{hook_lr},{hook_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    if hook:
        clean_hook = re.sub(r"[^\x20-\x7E]", "", hook).strip().upper()[:44]
        text = _esc(clean_hook)
        lines.append(
            f"Dialogue: 1,{_ass_time(0)},{_ass_time(min(3.5, clip_len))},Hook,,0,0,0,,"
            f"{{\\fad(120,250)}}{text}"
        )

    for group in _groups(words):
        for i, w in enumerate(group):
            s = w.start - clip_start
            e = (group[i + 1].start if i + 1 < len(group) else w.end) - clip_start
            e = min(e, clip_len)
            if e <= 0 or s >= clip_len or e - s < 0.02:
                continue
            parts = []
            for j, x in enumerate(group):
                tok = _esc(x.text.upper() if st["upper"] else x.text)
                if j == i:
                    parts.append(f"{{\\c{st['active']}\\fscx114\\fscy114\\t(0,80,\\fscx100\\fscy100)}}{tok}{{\\c&HFFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(tok)
            pop = "{\\fscx86\\fscy86\\t(0,90,\\fscx100\\fscy100)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{pop}{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"


# ---------- encode ----------

def _audio_tracks(video: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True,
    ).stdout.split()
    return len(out)


def _probe_size(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def render_clip(
    video: Path,
    words: list[Word],
    start: float,
    end: float,
    out_dir: Path,
    name: str,
    style: str = "yellow",
    layout: str = "auto",
    hook: str | None = None,
    aspect: str = DEFAULT_ASPECT,
    facecam: Box | None = None,
    keep: list[tuple[float, float]] | None = None,
    overlay_path: Path | None = None,
) -> dict:
    """Writes <name>.mp4 and <name>.jpg into out_dir. Returns what layout was used.
    `facecam` skips detection (used by tests and by a forced split).
    `keep`: parts of the clip to keep, in seconds from its start (see trim.py);
    everything else (pauses, fillers) is cut and the captions re-timed."""
    clip_len = end - start
    W, H = ASPECTS.get(aspect, ASPECTS[DEFAULT_ASPECT])
    clip_words = [w for w in words if w.end > start and w.start < end]
    ass_name = f"{name}.ass"
    src_w, src_h = _probe_size(video)
    src_r, out_r = src_w / src_h, W / H
    # Source already (close to) the target shape: just scale and trim the edges.
    # For 9:16 anything at or narrower than 0.6 qualifies, as before.
    vertical_already = src_r <= 0.6 if out_r < 0.6 else abs(src_r / out_r - 1) <= 0.07
    used = layout
    if src_r < out_r and not vertical_already:
        used = layout = "fit"  # source narrower than the output: a side crop can't fill it
    keys = None
    split = None
    tall = out_r < 1  # stacking two panels only makes sense in a tall frame
    if layout in ("auto", "fill", "split") and not vertical_already:
        boxes = detect_faces(video, start, end) if facecam is None else []
        if layout in ("auto", "split") and tall:
            cam = facecam or facecam_from_faces(boxes)
            split = split_layout(src_w, src_h, cam, W, H) if cam else None
            if split:
                used = "split"
        if split is None:
            if layout == "split":
                layout = "auto"  # asked for split but no facecam found: pick the next best layout
            keys = camera_path(track_samples(boxes)) if layout in ("auto", "fill") else None
            if layout == "auto":
                used = "fill" if keys else "fit"
            elif keys is None:
                keys = [(0.0, 0.5)]  # forced fill with no face: centre crop

    from . import trim

    if keep:
        out_len = sum(e - s for s, e in keep)
        cap_words, cap_start = trim.remap_words(clip_words, start, keep), 0.0
    else:
        out_len, cap_words, cap_start = clip_len, clip_words, start
    # Fallback attention-catching hook if no AI/campaign hook was supplied
    if hook is None:
        hook = "WATCH UNTIL THE LAST SECOND"

    (out_dir / ass_name).write_text(
        build_ass(cap_words, cap_start, out_len, style, hook, size=(W, H), hook_top=split["top_h"] if split else None),
        encoding="utf-8",
    )

    if split:
        cx, cy, cw, ch = split["cam"]
        gx, gy, gw, gh = split["game"]
        graph = (
            f"[0:v]split[a][b];"
            f"[a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{split['top_h']}:flags=lanczos,setsar=1[cam];"
            f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{split['bottom_h']}:flags=lanczos,setsar=1[game];"
            f"[cam][game]vstack,ass={ass_name}[v]"
        )
    elif vertical_already:
        used = "source"
        graph = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},ass={ass_name}[v]"
    elif used == "fill":
        crop_w = crop_width(src_w, src_h, W, H)
        x = _crop_x_expr(keys, src_w, crop_w)
        graph = f"[0:v]crop={crop_w}:{src_h}:x='{x}':y=0,scale={W}:{H}:flags=lanczos,ass={ass_name}[v]"
    else:  # fit: whole frame over a blurred copy of itself
        fg = f"{W}:-2" if src_r >= out_r else f"-2:{H}"  # fit the limiting side
        graph = (
            f"[0:v]split[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:4,eq=brightness=-0.12[bg];"
            f"[b]scale={fg}:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,ass={ass_name}[v]"
        )

    audio_tracks = _audio_tracks(video)
    audio_maps = ["0:a?"]
    af_args: list[str] = []
    if keep:
        # Cut after reframing (the crop's camera path uses the original clip time)
        # and before the captions (which are already re-timed). fps=30 first gives
        # a constant frame rate, so N/30 is the exact new timestamp.
        expr = trim.select_expr(keep)
        graph = graph.replace(f",ass={ass_name}[v]",
                              f",fps=30,select='{expr}',setpts=N/30/TB,ass={ass_name}[v]")
        # Trim every audio track: "[0:a]" alone would pick only the first one,
        # while the untrimmed path keeps them all.
        for i in range(audio_tracks):
            graph += f";[0:a:{i}]aselect='{expr}',asetpts=N/SR/TB,loudnorm=I=-14:TP=-1.5:LRA=11[a{i}]"
        audio_maps = [f"[a{i}]" for i in range(audio_tracks)]
    elif audio_tracks > 0:
        af_args = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.12"]

    has_ov = overlay_path is not None and overlay_path.is_file()
    if has_ov:
        ov_w, ov_h = _probe_size(overlay_path)
        full_ov = ov_h > 0 and (ov_w / ov_h) <= 0.65 and H > W
        is_vid_ov = overlay_path.suffix.lower() in (".mov", ".mp4", ".gif")
        ov_opts = "shortest=1:format=auto" if is_vid_ov else "eof_action=repeat:format=auto"
        graph = graph.replace("[v]", "[vpre]")
        if full_ov:
            graph += f";[1:v]scale={W}:{H}:flags=lanczos[ov];[vpre][ov]overlay=0:0:{ov_opts}[v]"
        else:
            bw = 240 if H > W else 200
            ty = 68 if H > W else 36
            graph += f";[1:v]scale={bw}:-1:flags=lanczos[ov];[vpre][ov]overlay=(W-w)/2:{ty}:{ov_opts}[v]"

    ov_inputs: list[str] = []
    if has_ov:
        if overlay_path.suffix.lower() in (".mov", ".mp4", ".gif"):
            ov_inputs = ["-stream_loop", "-1", "-i", str(overlay_path)]
        else:
            ov_inputs = ["-i", str(overlay_path)]

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{clip_len:.3f}", "-i", str(video),
        *ov_inputs,
        "-filter_complex", graph, "-map", "[v]", *[x for m in audio_maps for x in ("-map", m)], *af_args,
        "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", f"{name}.mp4",
    ]
    # cwd=out_dir so the ass filter gets a bare filename: Windows paths with
    # colons and apostrophes can't be escaped reliably inside a filter graph.
    proc = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(1.0, out_len / 2):.2f}", "-i", f"{name}.mp4",
         "-frames:v", "1", "-vf", "scale=360:-2", f"{name}.jpg"],
        cwd=out_dir, capture_output=True,
    )
    (out_dir / ass_name).unlink(missing_ok=True)
    return {"layout": used, "length": round(out_len, 2)}
