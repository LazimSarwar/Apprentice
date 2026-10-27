"""Shared helpers for the day-1 API probes: clients, video utilities, cost and result logging."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "probes" / "out"
load_dotenv(ROOT / ".env")

META_BASE_URL = "https://api.meta.ai/v1"
BASETEN_BASE_URL = "https://inference.baseten.co/v1"

# USD per 1M tokens (input, cached input, output), from the public model pages on 2026-10-27.
PRICES = {
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.002, 0.20),
    "muse-spark-open": (1.25, 0.15, 4.25),
}


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def video_info(path: Path) -> dict[str, Any]:
    """Duration, fps, frame count and size via ffprobe."""
    raw = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,nb_frames:format=duration,size",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(raw)
    stream = data["streams"][0]
    num, den = (int(x) for x in stream["r_frame_rate"].split("/"))
    return {
        "width": stream["width"],
        "height": stream["height"],
        "fps": num / den if den else None,
        "frames": int(stream["nb_frames"]) if stream.get("nb_frames", "N/A") != "N/A" else None,
        "duration_s": float(data["format"]["duration"]),
        "bytes": int(data["format"]["size"]),
    }


def small_mp4(path: Path, max_height: int = 720) -> Path:
    """Re-encode to H.264 at most `max_height`p without audio, so uploads stay well under size limits."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"{path.stem}.{max_height}p.mp4"
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


def keyframes(path: Path, count: int = 12, width: int = 640) -> list[tuple[float, Path]]:
    """Evenly spaced JPEG frames with their timestamps in seconds."""
    duration = video_info(path)["duration_s"]
    frame_dir = OUT_DIR / f"{path.stem}.frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(count):
        t = duration * (i + 0.5) / count
        target = frame_dir / f"{i:02d}.jpg"
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(target),
            ],
            check=True,
        )
        frames.append((round(t, 2), target))
    return frames


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def cost_usd(model: str, usage: Any) -> float | None:
    """Estimate request cost from an OpenAI-style usage object (chat or responses)."""
    if model.endswith("-contributor"):
        key = "muse-spark-1.3-contributor"
    elif "spark-open" in model.lower():
        key = "muse-spark-open"
    elif model.startswith("muse-spark-1.3"):
        key = "muse-spark-1.3"
    else:
        return None
    if usage is None:
        return None
    u = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    inp = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    out = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
    details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    cached = (details or {}).get("cached_tokens", 0) or 0
    p_in, p_cached, p_out = PRICES[key]
    return round(((inp - cached) * p_in + cached * p_cached + out * p_out) / 1_000_000, 6)


class Timer:
    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *_: Any) -> None:
        self.seconds = round(time.perf_counter() - self.start, 2)


def save(probe: str, payload: dict[str, Any]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"{probe}-{datetime.now():%Y%m%d-%H%M%S}.json"
    target.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return target


def error_text(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    return f"{type(exc).__name__}: {exc}" + (f" | body={body}" if body else "")
