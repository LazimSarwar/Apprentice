"""Probe 3: open-weights path. Which Muse Spark Open slug does our Baseten key accept, and does it take
video through a video_url part?

Usage:  python probes/p3_spark_open_video.py CLIP.mp4 [--effort low]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from common import (
    BASETEN_BASE_URL, Timer, cost_usd, data_url, error_text, require_env, save, small_mp4,
    video_info,
)
from p2_spark_video import PROMPT

# Docs list the first, the event lists the second; try whichever the key's model list confirms.
CANDIDATE_SLUGS = ["meta-models/muse-spark-open-1026", "meta-models/muse-spark-open"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("clip", type=Path)
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh"])
    args = parser.parse_args()

    api = OpenAI(base_url=BASETEN_BASE_URL, api_key=require_env("BASETEN_API_KEY"))
    report: dict[str, Any] = {"probe": "muse-spark-open video_url on Baseten", "source": str(args.clip)}

    try:
        listed = [m.id for m in api.models.list()]
        report["muse_models_listed"] = [m for m in listed if "muse" in m.lower()]
        print("muse models visible to this key:", report["muse_models_listed"] or "none")
    except Exception as exc:  # noqa: BLE001 - a probe reports every failure
        listed = []
        report["models_list_error"] = error_text(exc)
        print("model list FAILED:", report["models_list_error"])

    preferred = os.environ.get("BASETEN_MODEL", "").strip()
    slugs = [s for s in [preferred, *CANDIDATE_SLUGS] if s]
    slugs = sorted(dict.fromkeys(slugs), key=lambda s: s not in listed)  # listed slugs first

    clip = small_mp4(args.clip)
    report["uploaded"] = video_info(clip)
    video = data_url(clip, "video/mp4")
    for slug in slugs:
        try:
            with Timer() as timer:
                response = api.chat.completions.create(
                    model=slug,
                    reasoning_effort=args.effort,
                    max_tokens=16000,  # default 4096 can be eaten entirely by reasoning
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "video_url", "video_url": {"url": video}},
                    ]}],
                )
            message = response.choices[0].message
            reasoning = getattr(message, "reasoning_content", None) or ""
            report["result"] = {
                "slug": slug, "ok": True, "seconds": timer.seconds, "effort": args.effort,
                "finish_reason": response.choices[0].finish_reason,
                "reasoning_chars": len(reasoning), "text": message.content,
                "usage": response.usage, "cost_usd": cost_usd("muse-spark-open", response.usage),
            }
            print(f"[{slug}] OK in {timer.seconds}s, cost ${report['result']['cost_usd']}, "
                  f"reasoning {len(reasoning)} chars\n{message.content}")
            break
        except Exception as exc:  # noqa: BLE001
            report.setdefault("failures", []).append({"slug": slug, "error": error_text(exc)})
            print(f"[{slug}] FAILED: {error_text(exc)}")
    print("saved", save("p3-spark-open", report))


if __name__ == "__main__":
    main()
