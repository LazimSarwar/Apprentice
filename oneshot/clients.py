"""API configuration: load .env, build clients, estimate cost."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

META_BASE_URL = "https://api.meta.ai/v1"

# Open-weights (Muse Spark Open) inference provider. Fireworks is OpenAI-compatible;
# override the base URL / model via .env if needed.
FIREWORKS_BASE_URL = os.environ.get("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")
# Second open-weights provider, also OpenAI-compatible. The event key only serves the -1026 slug.
BASETEN_BASE_URL = os.environ.get("BASETEN_BASE_URL", "https://inference.baseten.co/v1")
BASETEN_DEFAULT_MODEL = "meta-models/muse-spark-open-1026"

# USD per 1M tokens (input, cached input, output), public model pages 2026-10-27.
_SPARK_PRICE = (1.25, 0.15, 4.25)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def spark_cost_usd(usage: Any) -> float | None:
    """Estimate a Spark request's cost from an OpenAI-style usage object."""
    if usage is None:
        return None
    u = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    inp = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    out = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
    details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    cached = (details or {}).get("cached_tokens", 0) or 0
    p_in, p_cached, p_out = _SPARK_PRICE
    return round(((inp - cached) * p_in + cached * p_cached + out * p_out) / 1_000_000, 6)
