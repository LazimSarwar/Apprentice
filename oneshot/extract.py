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
- Give each object a stable snake_case id built from its colour and what it is.
- `shape` is what the object visually looks like (block, sheet, mug, box, card...)."""

REPAIR = "That was not valid for the schema ({error}). Return corrected JSON only, same shape."

GUIDE = """
These objects were seen in a reference video of the same task. When one of them appears,
use exactly its id: {objects}. Only invent a new id for an object that is not in this list."""


SPOKEN = """
While recording, the person said: "{said}"
When they clearly name an object you can see (e.g. 'liner', 'charger'), use that name in its id
(e.g. 'blue_liner'); keep `shape` as what it looks like."""


def build_prompt(known_objects: list[dict] | None = None, spoken: str | None = None) -> str:
    """The extraction prompt, optionally guided to reuse a reference video's object ids
    so two videos of the same task can be compared step by step, and to name objects
    with the words spoken while recording."""
    prompt = PROMPT + (SPOKEN.format(said=spoken.replace('"', "'")) if spoken else "")
    if not known_objects:
        return prompt
    listed = ", ".join(f"{o['id']} ({o.get('color') or ''} {o.get('shape') or ''}".rstrip() + ")"
                       for o in known_objects)
    return prompt + GUIDE.format(objects=listed)

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


def _hosted_caller(clip: Path, model: str, prompt: str) -> tuple[Callable, str]:
    """Return a (call, file_id) for native-video extraction on the hosted API."""
    api = OpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY"))
    uploaded = api.files.create(file=(clip.name, clip.read_bytes(), "video/mp4"), purpose="user_data")
    base = [{"type": "input_text", "text": prompt}, {"type": "input_video", "file_id": uploaded.id}]

    def call(extra: str | None):
        content = base + ([{"type": "input_text", "text": extra}] if extra else [])
        resp = api.responses.create(
            model=model, reasoning={"effort": "low"},
            input=[{"type": "message", "role": "user", "content": content}],
        )
        return resp.output_text, resp.usage

    return call, uploaded.id


def _fireworks_caller(clip: Path, model: str, prompt: str) -> tuple[Callable, None]:
    """Return a (call, None) for keyframe extraction on Muse Spark Open via Fireworks."""
    api = OpenAI(base_url=FIREWORKS_BASE_URL, api_key=require_env("FIREWORKS_API_KEY"))
    frames = keyframes(clip, ROOT / "runs" / "frames", count=KEYFRAMES)
    base: list[dict] = [{"type": "text", "text": prompt + "\nThe video is given as evenly spaced frames."}]
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


def extract_graph(clip: Path, provider: str = "hosted", model: str | None = None,
                  known_objects: list[dict] | None = None,
                  spoken: str | None = None) -> tuple[RawGraph, dict[str, Any]]:
    """Return the raw task graph and metadata (provider, model, seconds, cost, file_id).

    `known_objects` (id/color/shape dicts from a reference graph) guides the model to
    reuse the same ids, so this video can be checked against the reference; `spoken` (what
    was said while recording) lets it name objects in the person's own words.
    """
    prompt = build_prompt(known_objects, spoken)
    if provider == "hosted":
        model = model or MODEL
        call, file_id = _hosted_caller(clip, model, prompt)
    elif provider == "fireworks":
        model = model or require_env("FIREWORKS_MODEL")
        call, file_id = _fireworks_caller(clip, model, prompt)
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
