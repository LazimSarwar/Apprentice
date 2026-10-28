"""Step 1 of P0: read the task out of the video with a Muse Spark model.

Two providers:
  hosted    — Muse Spark 1.3 on the hosted API, native video (Responses + input_video).
  fireworks — Muse Spark Open (open weights) on Fireworks, evenly spaced keyframes as
              images (Chat Completions). This is the open-weights path for the bonus;
              it swaps only the extractor — SAM tracking stays on the hosted API.

Both share the JSON prompt, lenient parse, Pydantic validation, and one repair retry.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI
from pydantic import ValidationError

from .clients import FIREWORKS_BASE_URL, META_BASE_URL, ROOT, require_env, spark_cost_usd
from .media import data_url, keyframes
from .schema import ACTIONS, RELATIONS, RawGraph

MODEL = "muse-spark-1.3"
KEYFRAMES = 12

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
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    match = _JSON.search(text)
    if not match:
        raise ExtractionError(f"no JSON object in model output: {text[:200]!r}")
    return json.loads(match.group(0))


def _hosted_caller(clip: Path, model: str) -> tuple[Callable, str]:
    """Return a (call, file_id) for native-video extraction on the hosted API."""
    api = OpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY"))
    uploaded = api.files.create(file=(clip.name, clip.read_bytes(), "video/mp4"), purpose="user_data")
    base = [{"type": "input_text", "text": PROMPT}, {"type": "input_video", "file_id": uploaded.id}]

    def call(extra: str | None):
        content = base + ([{"type": "input_text", "text": extra}] if extra else [])
        resp = api.responses.create(
            model=model, reasoning={"effort": "low"},
            input=[{"type": "message", "role": "user", "content": content}],
        )
        return resp.output_text, resp.usage

    return call, uploaded.id


def _fireworks_caller(clip: Path, model: str) -> tuple[Callable, None]:
    """Return a (call, None) for keyframe extraction on Muse Spark Open via Fireworks."""
    api = OpenAI(base_url=FIREWORKS_BASE_URL, api_key=require_env("FIREWORKS_API_KEY"))
    frames = keyframes(clip, ROOT / "runs" / "frames", count=KEYFRAMES)
    base: list[dict] = [{"type": "text", "text": PROMPT + "\nThe video is given as evenly spaced frames."}]
    for t, frame in frames:
        base.append({"type": "text", "text": f"Frame at t={t}s:"})
        base.append({"type": "image_url", "image_url": {"url": data_url(frame, "image/jpeg")}})

    def call(extra: str | None):
        content = base + ([{"type": "text", "text": extra}] if extra else [])
        resp = api.chat.completions.create(
            model=model, messages=[{"role": "user", "content": content}], max_tokens=8192,
        )
        return resp.choices[0].message.content, resp.usage

    return call, None


def extract_graph(clip: Path, provider: str = "hosted",
                  model: str | None = None) -> tuple[RawGraph, dict[str, Any]]:
    """Return the raw task graph and metadata (provider, model, seconds, cost, file_id)."""
    if provider == "hosted":
        model = model or MODEL
        call, file_id = _hosted_caller(clip, model)
    elif provider == "fireworks":
        model = model or require_env("FIREWORKS_MODEL")
        call, file_id = _fireworks_caller(clip, model)
    else:
        raise ExtractionError(f"unknown provider '{provider}' (use 'hosted' or 'fireworks')")

    start = time.perf_counter()
    cost = 0.0
    last_error = ""
    for attempt in range(2):
        text, usage = call(REPAIR.format(error=last_error) if attempt else None)
        cost += spark_cost_usd(usage) or 0.0
        try:
            graph = RawGraph.model_validate(_loads(text))
        except (ExtractionError, ValidationError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if attempt == 0:
                continue
            raise ExtractionError(f"model output failed validation twice: {last_error}") from exc
        return graph, {
            "provider": provider,
            "model": model,
            "file_id": file_id,
            "extract_seconds": round(time.perf_counter() - start, 2),
            "extract_cost_usd": round(cost, 6),
        }
    raise ExtractionError(last_error)  # unreachable
