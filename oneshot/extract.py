"""Step 1 of P0: read the task out of the video with Muse Spark 1.3.

Uses the Responses API with an uploaded clip (`input_video` + file_id) — the path the
day-1 probe confirmed. The model returns JSON; we parse leniently, validate against
`RawGraph`, and give it exactly one chance to repair malformed output before failing.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from .clients import META_BASE_URL, require_env, spark_cost_usd
from .schema import ACTIONS, RELATIONS, RawGraph

MODEL = "muse-spark-1.3"

PROMPT = f"""You are watching a single demonstration of a tabletop task.
Return ONLY JSON (no prose, no markdown fences) with this exact shape:
{{"objects": [{{"id": "red_block", "color": "red", "shape": "block"}}],
  "steps": [{{"order": 1, "start_s": 0.0, "end_s": 1.5, "action": "push",
             "object": "red_block", "target": "gray_tray", "relation": "inside"}}],
  "final_relations": [{{"relation": "inside", "a": "red_block", "b": "gray_tray"}}]}}

Rules:
- Use only these actions: {", ".join(ACTIONS)}.
- Use only these relations (or null): {", ".join(RELATIONS)}.
- `order` is a 1-based integer in the order the actions happen.
- Times are seconds from the start of the video; start_s < end_s.
- Every `object` and `target` must be an id that appears in `objects` (target may be null).
- Give each object a stable snake_case id built from its colour and shape."""

REPAIR = "That was not valid for the schema ({error}). Return corrected JSON only, same shape."

_JSON = re.compile(r"\{.*\}", re.DOTALL)


class ExtractionError(RuntimeError):
    pass


def _loads(text: str) -> dict[str, Any]:
    """Parse a JSON object out of the model's reply, tolerating stray prose or fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    match = _JSON.search(text)
    if not match:
        raise ExtractionError(f"no JSON object in model output: {text[:200]!r}")
    return json.loads(match.group(0))


def extract_graph(clip: Path, model: str = MODEL) -> tuple[RawGraph, dict[str, Any]]:
    """Return the raw task graph and metadata (seconds, cost, model)."""
    api = OpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY"))
    uploaded = api.files.create(
        file=(clip.name, clip.read_bytes(), "video/mp4"), purpose="user_data"
    )
    base_content: list[dict[str, Any]] = [
        {"type": "input_text", "text": PROMPT},
        {"type": "input_video", "file_id": uploaded.id},
    ]

    start = time.perf_counter()
    cost = 0.0
    last_error = ""
    for attempt in range(2):
        # On the repair attempt, resend the prompt and video plus the error so the
        # model has full context (each call is stateless).
        content = base_content
        if attempt == 1:
            content = base_content + [
                {"type": "input_text", "text": REPAIR.format(error=last_error)}
            ]
        response = api.responses.create(
            model=model,
            reasoning={"effort": "low"},
            input=[{"type": "message", "role": "user", "content": content}],
        )
        cost += spark_cost_usd(response.usage) or 0.0
        text = response.output_text
        try:
            graph = RawGraph.model_validate(_loads(text))
        except (ExtractionError, ValidationError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if attempt == 0:
                continue
            raise ExtractionError(f"model output failed validation twice: {last_error}") from exc

        meta = {
            "model": model,
            "file_id": uploaded.id,
            "extract_seconds": round(time.perf_counter() - start, 2),
            "extract_cost_usd": round(cost, 6),
        }
        return graph, meta

    raise ExtractionError(last_error)  # unreachable, keeps type checkers happy
