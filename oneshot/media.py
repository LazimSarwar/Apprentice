"""Video helpers for the pipeline: probe metadata and shrink clips for upload.

Self-contained (depends only on ffmpeg/ffprobe on PATH) so nothing in the product
package reaches back into the day-1 probes.
"""

from __future__ import annotations

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
