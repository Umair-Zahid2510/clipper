"""30-second clip extraction, 9:16 vertical layout rendering, and mandatory logo overlay via ffmpeg."""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import config
from recorder import get_segments_for_window, segment_time_bounds


def _write_concat_list(segments: list[Path], work_dir: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(prefix="concat_", suffix=".txt", dir=str(work_dir))
    concat_path = Path(tmp_name)
    lines = ["ffconcat version 1.0"]
    for seg in segments:
        escaped = seg.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    with open(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return concat_path


def probe_duration(video_path: Path) -> float:
    """Return video duration in seconds via ffprobe."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(proc.stdout.strip())


def _probe_dims(path: Path) -> tuple[int, int] | None:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def extract_clip(
    sources: Path | list[Path],
    out_path: Path,
    start_offset: float = 0.0,
    duration: float = 30.0,
    vertical_mode: bool = True,
    vertical_layout: str = "blur",
    overlay_path: Path | None = None,
    use_default_overlay: bool = True,
) -> Path:
    """Extract a clip from a video file or list of .ts segments, apply optional logo overlay, and encode to MP4."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if overlay_path is None and use_default_overlay:
        overlay_path = config.get_overlay_path()
    has_overlay = overlay_path is not None and overlay_path.is_file()
    is_looped_video_overlay = bool(has_overlay and overlay_path.suffix.lower() in (".mov", ".mp4", ".gif"))

    concat_file: Path | None = None
    try:
        if isinstance(sources, list):
            if not sources:
                raise ValueError("No source video segments provided.")
            concat_file = _write_concat_list(sources, config.TEMP_DIR)
            input_args = ["-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(concat_file)]
        else:
            input_args = ["-i", str(sources)]

        cmd = ["ffmpeg", "-y", "-loglevel", "error", *input_args]
        if has_overlay:
            if is_looped_video_overlay:
                cmd += ["-stream_loop", "-1", "-i", str(overlay_path)]
            else:
                cmd += ["-i", str(overlay_path)]

        out_w, out_h = (1080, 1920) if vertical_mode else (1920, 1080)
        if vertical_mode:
            if vertical_layout == "crop":
                base_graph = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[base]"
            else:
                base_graph = (
                    "[0:v]split[a][b];"
                    "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                    "boxblur=24:4,eq=brightness=-0.12[bg];"
                    "[b]scale=1080:-2:flags=lanczos[fg];"
                    "[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
                )
        else:
            base_graph = "[0:v]null[base]"

        if has_overlay:
            dims = _probe_dims(overlay_path)
            is_full_frame = bool(dims and dims[1] > 0 and (dims[0] / dims[1]) <= 0.65 and vertical_mode)
            ov_opts = "shortest=1:format=auto" if is_looped_video_overlay else "eof_action=repeat:format=auto"
            if is_full_frame:
                ov_graph = f";[1:v]scale={out_w}:{out_h}:flags=lanczos[ov];[base][ov]overlay=0:0:{ov_opts}[v]"
            else:
                badge_w = 240 if vertical_mode else 200
                top_y = 68 if vertical_mode else 36
                ov_graph = (
                    f";[1:v]scale={badge_w}:-1:flags=lanczos[ov];"
                    f"[base][ov]overlay=(W-w)/2:{top_y}:{ov_opts}[v]"
                )
            cmd += ["-filter_complex", base_graph + ov_graph, "-map", "[v]", "-map", "0:a?"]
        else:
            if vertical_mode:
                cmd += ["-filter_complex", base_graph.replace("[base]", "[v]"), "-map", "[v]", "-map", "0:a?"]
            else:
                cmd += ["-map", "0:v:0", "-map", "0:a?"]

        # Output seek/duration options MUST come after all -i inputs
        cmd += [
            "-ss", f"{max(0.0, start_offset):.3f}",
            "-t", f"{duration:.3f}",
            "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg extract failed: {proc.stderr.strip()[-400:]}")
        return out_path
    finally:
        if concat_file is not None:
            concat_file.unlink(missing_ok=True)


def extract_spike_from_buffer(
    spike_ts: float,
    temp_dir: Path,
    out_path: Path,
    wait_seconds: float = config.POST_SPIKE_WAIT_SECONDS,
    vertical_mode: bool = config.VERTICAL_MODE,
    vertical_layout: str = config.VERTICAL_LAYOUT,
    overlay_path: Path | None = None,
    use_default_overlay: bool = True,
) -> Path:
    """Wait for post-reaction footage, then cut [T - 15s, T + 15s] from buffered segments."""
    remaining = (spike_ts + wait_seconds) - time.time()
    if remaining > 0:
        time.sleep(remaining)

    segments = get_segments_for_window(temp_dir, spike_ts, pre_sec=15.0, post_sec=15.0)
    if not segments:
        raise FileNotFoundError(f"No buffered segments found around timestamp {spike_ts:.1f}.")

    first_start, _ = segment_time_bounds(segments[0], None, config.SEGMENT_SECONDS)
    start_offset = max(0.0, (spike_ts - 15.0) - first_start)
    # Never seek past the available buffer duration
    max_possible_offset = max(0.0, (len(segments) * config.SEGMENT_SECONDS) - 30.0)
    start_offset = min(start_offset, max_possible_offset)
    return extract_clip(
        segments,
        out_path,
        start_offset=start_offset,
        duration=30.0,
        vertical_mode=vertical_mode,
        vertical_layout=vertical_layout,
        overlay_path=overlay_path,
        use_default_overlay=use_default_overlay,
    )
