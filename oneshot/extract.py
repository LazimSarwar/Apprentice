"""Step 1 of P0: read the task out of the video with a Muse Spark model.

Three providers:
  hosted    — Muse Spark 1.3 on the hosted API, native video (Responses + input_video).
  baseten   — Muse Spark Open (open weights) on Baseten, native video (Chat Completions
              + video_url).
  fireworks — Muse Spark Open on Fireworks, evenly spaced keyframes as images.
The two open-weights hosts are the path for the bonus; they swap only the extractor —
SAM tracking stays on the hosted API.

All share the JSON prompt, lenient parse, Pydantic validation, and one repair retry.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI
from pydantic import ValidationError

from .clients import (
    BASETEN_BASE_URL, BASETEN_DEFAULT_MODEL, FIREWORKS_BASE_URL, META_BASE_URL, MODEL_TIMEOUT_SECONDS, ROOT, require_env,
    spark_cost_usd, record_usage,
)
from .media import data_url, keyframes
from .schema import ACTIONS, RELATIONS, RawGraph

MODEL = "muse-spark-1.3"
KEYFRAMES = 12

PROMPT = f"""You are watching a single demonstration of a tabletop task.
Return ONLY JSON (no prose, no markdown fences) with this exact shape:
{{"objects": [{{"id": "red_block", "color": "red", "shape": "block", "kind": "solid", "tracking_description": "red block"}}],
  "steps": [{{"order": 1, "start_s": 0.0, "end_s": 1.5, "action": "push",
             "object": "red_block", "target": "gray_tray", "relation": "inside"}}],
  "final_relations": [{{"relation": "inside", "a": "red_block", "b": "gray_tray"}}]}}

Rules:
- Discover the actual task-relevant objects visible in this video. The names in the
  JSON example are examples, not a fixed object list. Include manipulated items and
  their supports/containers; do not invent an item because the task normally needs it.
- Use only these actions: {", ".join(ACTIONS)}.
- Use only these relations (or null): {", ".join(RELATIONS)}.
- `order` is a 1-based integer in the order the actions happen.
- Times are seconds from the start of the video; start_s < end_s.
- Every `object` and `target` must be an id that appears in `objects` (target may be null).
- Give each object a stable snake_case id built from its colour and what it is.
- `shape` is what the object visually looks like (block, sheet, mug, box, card...).
- `kind` is solid, container, or surface, based on visible physical structure.
  A cup or jar can be a container; a closed solid object is not a container merely
  because its name resembles one. Use null if the structure is unclear.
- `tracking_description` is a short visual noun phrase for locating that particular
  object: use its recognizable physical type and visible distinguishing features,
  e.g. "white charger", "white paper sheet", "white card with a blue logo".
  Avoid reducing recognizable objects to generic geometry such as "white block".
  Do not include actions, expected locations, order, or invisible properties. Do not
  invent distinguishing features when two objects look identical.
- Distinguish solid cubes/blocks from hollow containers. Use on for stacking a solid block,
  and inside only when an object actually goes into a container. Do not call a solid block a box.
- For each placement, name the immediate supporting object when it is visible. An item
  resting on a sheet within a container has the step relation on the sheet. Describe the
  enclosing container separately in final_relations when that relation is also visible.
  Being inside a container alone does not establish which object supports the item.
  If the support is hidden or unclear, report only the placement you can actually see.
- At most 16 objects and 128 steps. Include the final placements even when an earlier correct
  placement was undone later."""

REPAIR = "That was not valid for the schema ({error}). Return corrected JSON only, same shape."

GUIDE = """
These objects were seen in a reference video of the same task. When one of them appears,
reuse its id only when its visible appearance supports the identity: {objects}.
This is an identity guide, not a list of objects that must be present. Omit absent objects.
Give genuinely new or uncertain objects their own ids instead of forcing a reference match.
Do not substitute a different item just because it has the same colour. Observe the current
video afresh: describe what is visible now, including new objects and changed placements.
Never copy a reference appearance feature that is not visible in the current video."""


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
    fields = {"id", "color", "shape", "kind", "tracking_description"}
    listed = json.dumps([{k: v for k, v in o.items() if k in fields and v is not None}
                         for o in known_objects], ensure_ascii=False)
    return prompt + GUIDE.format(objects=listed)

_JSON = re.compile(r"\{.*\}", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

# Open-weights hosts: (base URL, key env var, default model, takes native video). Baseten
# accepts the whole clip as a video_url (probe 3); Fireworks gets keyframes. Spark Open always
# reasons, and its reasoning counts against max_tokens, so leave room for it on top of the JSON.
OPEN_HOSTS = {
    "fireworks": (FIREWORKS_BASE_URL, "FIREWORKS_API_KEY", None, False),
    "baseten": (BASETEN_BASE_URL, "BASETEN_API_KEY", BASETEN_DEFAULT_MODEL, True),
}
OPEN_MAX_TOKENS = 16000


class ExtractionError(RuntimeError):
    pass


def _loads(text: str) -> dict[str, Any]:
    """Parse a JSON object out of the model's reply, tolerating stray prose or fences."""
    text = _THINK.sub("", text or "").strip()  # reasoning inlined in the reply, if a host does that
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    match = _JSON.search(text)
    if not match:
        raise ExtractionError(f"no JSON object in model output: {text[:200]!r}")
    return json.loads(match.group(0))


def _hosted_caller(clip: Path, model: str, prompt: str, effort: str = "low") -> tuple[Callable, str]:
    """Return a (call, file_id) for native-video extraction on the hosted API."""
    api = OpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY"),
                 timeout=MODEL_TIMEOUT_SECONDS, max_retries=0)
    uploaded = api.files.create(file=(clip.name, clip.read_bytes(), "video/mp4"), purpose="user_data")
    base = [{"type": "input_text", "text": prompt}, {"type": "input_video", "file_id": uploaded.id}]

    def call(extra: str | None):
        content = base + ([{"type": "input_text", "text": extra}] if extra else [])
        resp = api.responses.create(
            model=model, reasoning={"effort": effort}, max_output_tokens=8192,
            input=[{"type": "message", "role": "user", "content": content}],
        )
        return resp.output_text, resp.usage

    return call, uploaded.id


def _open_caller(clip: Path, model: str, prompt: str, host: str) -> tuple[Callable, None]:
    """Return a (call, None) for extraction on Muse Spark Open via an open-weights host:
    the whole clip where the host takes video, else evenly spaced keyframes."""
    base_url, key_env, _, native_video = OPEN_HOSTS[host]
    api = OpenAI(base_url=base_url, api_key=require_env(key_env), timeout=MODEL_TIMEOUT_SECONDS, max_retries=0)
    if native_video:
        base: list[dict] = [{"type": "text", "text": prompt},
                            {"type": "video_url", "video_url": {"url": data_url(clip, "video/mp4")}}]
    else:
        frames = keyframes(clip, ROOT / "runs" / "frames", count=KEYFRAMES)
        base = [{"type": "text", "text": prompt + "\nThe video is given as evenly spaced frames."}]
        for t, frame in frames:
            base.append({"type": "text", "text": f"Frame at t={t}s:"})
            base.append({"type": "image_url", "image_url": {"url": data_url(frame, "image/jpeg")}})

    def call(extra: str | None):
        content = base + ([{"type": "text", "text": extra}] if extra else [])
        resp = api.chat.completions.create(
            model=model, messages=[{"role": "user", "content": content}], max_tokens=OPEN_MAX_TOKENS,
        )
        return resp.choices[0].message.content, resp.usage

    return call, None


def ask_about_clip(clip: Path, prompt: str, effort: str = "low", model: str = MODEL) -> str:
    """One question about one clip on the hosted model, recorded against the budget."""
    call, _ = _hosted_caller(clip, model, prompt, effort)
    text, usage = call(None)
    record_usage(spark_cost_usd(usage))
    return text or ""


def extract_graph(clip: Path, provider: str = "hosted", model: str | None = None,
                  known_objects: list[dict] | None = None,
                  spoken: str | None = None, effort: str = "low") -> tuple[RawGraph, dict[str, Any]]:
    """Return the raw task graph and metadata (provider, model, seconds, cost, file_id).

    `known_objects` (id/color/shape dicts from a reference graph) guides the model to
    reuse the same ids, so this video can be checked against the reference; `spoken` (what
    was said while recording) lets it name objects in the person's own words. `effort` is
    the hosted model's reasoning effort ("minimal" about halves a read, for near-live checks).
    """
    prompt = build_prompt(known_objects, spoken)
    if provider == "hosted":
        model = model or MODEL
        call, file_id = _hosted_caller(clip, model, prompt, effort)
    elif provider in OPEN_HOSTS:
        model_env = f"{provider.upper()}_MODEL"
        model = (model or os.environ.get(model_env, "").strip() or OPEN_HOSTS[provider][2]
                 or require_env(model_env))
        call, file_id = _open_caller(clip, model, prompt, provider)
    else:
        raise ExtractionError(f"unknown provider '{provider}' (use 'hosted', {', '.join(map(repr, OPEN_HOSTS))})")

    start = time.perf_counter()
    cost = 0.0
    last_error = ""
    for attempt in range(2):
        text, usage = call(REPAIR.format(error=last_error) if attempt else None)
        billed = spark_cost_usd(usage)
        record_usage(billed)
        cost += billed or 0.0
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
