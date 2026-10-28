"""Video helpers for the pipeline: probe metadata and shrink clips for upload.

Self-contained (depends only on ffmpeg/ffprobe on PATH) so nothing in the product
package reaches back into the day-1 probes.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

from .schema import VideoInfo


def video_info(path: Path) -> VideoInfo:
    """Duration, fps, frame count and size via ffprobe."""
    raw = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,nb_frames:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
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
    target = out_dir / f"{path.stem}.{max_height}p.mp4"
    if not target.exists():
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(path),
                "-vf", f"scale=-2:'min({max_height},ih)'", "-an",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", str(target),
            ],
            check=True,
        )
    return target


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
            check=True,
        )
        frames.append((round(t, 2), target))
    return frames


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
