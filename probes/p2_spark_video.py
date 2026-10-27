"""Probe 2: does hosted Muse Spark 1.3 accept video, and can it list the steps of a tabletop demo?

Tries three input paths, independently, and records which ones work, their latency and cost:
  A. Responses API, Files API upload, {"type": "input_video", "file_id": ...}
  B. Chat Completions, {"type": "video_url", "video_url": {"url": <data URL>}}
  C. Chat Completions, evenly spaced keyframes as image_url parts (documented fallback)

Usage:  python probes/p2_spark_video.py CLIP.mp4 [--only A|B|C] [--model muse-spark-1.3]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from openai import OpenAI

from common import (
    META_BASE_URL, Timer, cost_usd, data_url, error_text, keyframes, require_env, save,
    small_mp4, video_info,
)

PROMPT = """This video shows a person moving objects on a table.
Return only JSON, no prose, with this shape:
{"objects": [{"id": "red_block", "color": "red", "shape": "block"}],
 "steps": [{"order": 1, "start_s": 0.0, "end_s": 1.5, "action": "pick|place|stack|push",
            "object": "red_block", "target": "bowl or null", "relation": "on|inside|near|null"}],
 "final_relations": [{"relation": "inside", "a": "red_block", "b": "bowl"}]}
Use only the actions pick, place, stack, push. Times are seconds from the start of the video."""


def attempt_a(api: OpenAI, model: str, clip: Path) -> dict[str, Any]:
    uploaded = api.files.create(file=(clip.name, clip.read_bytes(), "video/mp4"), purpose="user_data")
    response = api.responses.create(
        model=model,
        reasoning={"effort": "low"},
        input=[{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": PROMPT},
                        {"type": "input_video", "file_id": uploaded.id}],
        }],
    )
    return {"file_id": uploaded.id, "text": response.output_text, "usage": response.usage}


def attempt_b(api: OpenAI, model: str, clip: Path) -> dict[str, Any]:
    response = api.chat.completions.create(
        model=model,
        reasoning_effort="low",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "video_url", "video_url": {"url": data_url(clip, "video/mp4")}},
        ]}],
    )
    return {"text": response.choices[0].message.content, "usage": response.usage}


def attempt_c(api: OpenAI, model: str, clip: Path) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT + "\nThe video is given as frames."}]
    for t, frame in keyframes(clip):
        content.append({"type": "text", "text": f"Frame at t={t}s:"})
        content.append({"type": "image_url", "image_url": {"url": data_url(frame, "image/jpeg")}})
    response = api.chat.completions.create(
        model=model, reasoning_effort="low", messages=[{"role": "user", "content": content}],
    )
    return {"frames": len(content) // 2, "text": response.choices[0].message.content,
            "usage": response.usage}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("clip", type=Path)
    parser.add_argument("--only", choices=["A", "B", "C"])
    parser.add_argument("--model", default="muse-spark-1.3")
    args = parser.parse_args()

    clip = small_mp4(args.clip)
    api = OpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY"))
    report: dict[str, Any] = {"probe": f"{args.model} video input", "source": str(args.clip),
                              "uploaded": video_info(clip), "attempts": {}}
    attempts = {"A": attempt_a, "B": attempt_b, "C": attempt_c}
    for name, run in attempts.items():
        if args.only and name != args.only:
            continue
        try:
            with Timer() as timer:
                result = run(api, args.model, clip)
            result.update(ok=True, seconds=timer.seconds, cost_usd=cost_usd(args.model, result["usage"]))
            print(f"[{name}] OK in {timer.seconds}s, cost ${result['cost_usd']}\n{result['text']}\n")
        except Exception as exc:  # noqa: BLE001 - a probe reports every failure
            result = {"ok": False, "error": error_text(exc)}
            print(f"[{name}] FAILED: {result['error']}\n")
        report["attempts"][name] = result
    print("saved", save("p2-spark", report))


if __name__ == "__main__":
    main()
