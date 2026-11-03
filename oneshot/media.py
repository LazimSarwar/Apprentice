"""Video helpers for the pipeline: probe metadata and shrink clips for upload.

Self-contained (depends only on ffmpeg/ffprobe on PATH) so nothing in the product
package reaches back into the day-1 probes.
"""

from __future__ import annotations

import base64
import json
import hashlib
import math
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .schema import VideoInfo

MEDIA_TIMEOUT_SECONDS = float(os.environ.get("MEDIA_TIMEOUT_SECONDS", "90"))
PROBE_TIMEOUT_SECONDS = 15


def source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scale(height: int) -> str:
    return f"scale=w='min(1280,iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"


def _encode(command: list[str], target: Path) -> Path:
    """A failed or timed-out encode never publishes a cache entry."""
    tmp = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.tmp{target.suffix}")
    try:
        subprocess.run([*command, str(tmp)], check=True, timeout=MEDIA_TIMEOUT_SECONDS)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def video_info(path: Path) -> VideoInfo:
    """Duration, fps, frame count and size via ffprobe."""
    raw = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,nb_frames:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True, timeout=PROBE_TIMEOUT_SECONDS,
    ).stdout
    data = json.loads(raw)
    stream = data["streams"][0]
    num, den = (int(x) for x in stream["r_frame_rate"].split("/"))
    frames = stream.get("nb_frames", "N/A")
    return VideoInfo(
        width=stream["width"],
        height=stream["height"],
        fps=(num / den) if den else None,
        frames=int(frames) if frames != "N/A" else None,
        duration_s=float(data["format"]["duration"]),
    )


def small_mp4(path: Path, out_dir: Path, max_height: int = 720) -> Path:
    """Re-encode to H.264 at most `max_height`p, no audio, keeping fps.

    SAM and Spark both work on the re-encoded clip, so its fps is what frame
    indices map onto — see :func:`frame_to_seconds`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{path.stem}.{source_digest(path)[:20]}.{max_height}p.mp4"
    if not target.exists():
        _encode(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(path),
                "-vf", _scale(max_height), "-an",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
            ],
            target,
        )
    return target


def cut(path: Path, seconds: float | None, out: Path, max_height: int = 480, fps: int = 30) -> Path:
    """The first `seconds` of a video (all of it when None) as a small H.264 clip — what a
    near-live read sends. Also turns a browser recording (WebM, no duration) into an MP4.
    The video model samples frames at its own rate, so a low `fps` costs it nothing and
    makes the upload smaller."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("Clip duration must be positive and finite")
    limit = seconds if seconds is not None else float(os.environ.get("MAX_UPLOAD_SECONDS", "120")) + 1
    head = ["-t", f"{limit:.2f}"]
    _encode(
        ["ffmpeg", "-y", "-v", "error", *head, "-i", str(path),
         "-vf", _scale(max_height), "-an", "-r", str(fps),
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "26"],
        out,
    )
    return out


def segment(path: Path, start: float, end: float, out: Path, max_height: int = 720) -> Path:
    """One stretch of a video as a small H.264 clip, for a closer look at a single moment."""
    if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
        raise ValueError("A clip needs a finite start before its end")
    out.parent.mkdir(parents=True, exist_ok=True)
    _encode(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(path),
         "-vf", _scale(max_height), "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "26"],
        out,
    )
    return out


def frame_to_seconds(frame_index: int, fps: float | None) -> float:
    """Map a SAM frame index back to a timestamp on the uploaded clip."""
    if not fps:
        return 0.0
    return round(frame_index / fps, 3)


def keyframes(path: Path, out_dir: Path, count: int = 12, width: int = 640) -> list[tuple[float, Path]]:
    """Evenly spaced JPEG frames with their timestamps — the open-weights input path,
    for providers that take images rather than native video."""
    duration = video_info(path).duration_s
    frame_dir = out_dir / f"{path.stem}.frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[float, Path]] = []
    for i in range(count):
        t = duration * (i + 0.5) / count
        target = frame_dir / f"{i:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(target)],
            check=True, timeout=MEDIA_TIMEOUT_SECONDS,
        )
        frames.append((round(t, 2), target))
    return frames


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
