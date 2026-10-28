"""Step 2 of P0: track each object with SAM 3.1 to produce the evidence.

Adapted from the day-1 probe, whose request shape follows Meta's MIT-licensed cookbook
recipe 07_sam/01_api_basics (credited in the README). Tracks every object concept
concurrently, then keeps the longest-lived trajectory per concept as its track.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncIterable, AsyncIterator

# SAM 3.1 runs on the hosted API regardless of which model extracted the task graph.

from meta_sam_parser import parse_responses_stream, video_segmentation_format
from openai import AsyncOpenAI

from .clients import META_BASE_URL, require_env
from .media import frame_to_seconds
from .schema import Box, Track

MODEL = "sam-3.1"


async def _finalized_events(stream: AsyncIterable[Any]) -> AsyncIterator[dict[str, Any]]:
    """The API emits response.content_part.done but not response.output_text.done,
    which the parser needs to close a text lane — synthesize it (cookbook fix)."""
    closed: set[tuple[object, object, object]] = set()
    async for event in stream:
        data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
        lane = (data.get("item_id"), data.get("output_index"), data.get("content_index"))
        if data.get("type") == "response.output_text.done":
            if lane not in closed:
                closed.add(lane)
                yield data
            continue
        yield data
        part = data.get("part")
        if (
            data.get("type") == "response.content_part.done"
            and lane not in closed
            and isinstance(part, dict)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        ):
            closed.add(lane)
            yield {
                "type": "response.output_text.done",
                "item_id": data.get("item_id"),
                "output_index": data.get("output_index"),
                "content_index": data.get("content_index"),
                "text": part["text"],
            }


async def _track_one(
    api: AsyncOpenAI, file_id: str, concept: str, fps: float | None
) -> Track | None:
    """Track a single concept; return the longest trajectory as its Track, or None."""
    stream = await api.responses.create(
        model=MODEL,
        stream=True,
        input=[{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": concept},
                {"type": "input_video", "file_id": file_id},
            ],
        }],
    )
    async with stream, aclosing(_finalized_events(stream)) as events:
        parsed = parse_responses_stream(events, video_segmentation_format())
        async with parsed:
            async for _ in parsed:
                pass
            result = await parsed.final_result()

    boxes_by_object: dict[str, list[Box]] = defaultdict(list)
    for record in result.records:
        if record.kind == "box" and record.frame is not None:
            idx = record.frame.frame_index
            boxes_by_object[record.object_id].append(
                Box(
                    frame=idx,
                    t_s=frame_to_seconds(idx, fps),
                    box=[record.left, record.top, record.right, record.bottom],
                    confidence=record.confidence,
                )
            )
    if not boxes_by_object:
        return None

    # SAM may return several object ids for one concept; keep the longest-lived one.
    object_id, boxes = max(boxes_by_object.items(), key=lambda kv: len(kv[1]))
    boxes.sort(key=lambda b: b.frame)
    return Track(
        concept=concept,
        sam_object_id=object_id,
        first_s=boxes[0].t_s,
        last_s=boxes[-1].t_s,
        visible_frames=len(boxes),
        boxes=boxes,
    )


async def _track_all(
    clip: Path, concepts: list[str], fps: float | None, file_id: str | None
) -> dict[str, Track | None]:
    async with AsyncOpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY")) as api:
        if file_id is None:                      # extractor didn't upload (e.g. open-weights path)
            uploaded = await api.files.create(
                file=(clip.name, clip.read_bytes(), "video/mp4"), purpose="user_data"
            )
            file_id = uploaded.id
        results = await asyncio.gather(*(_track_one(api, file_id, c, fps) for c in concepts))
    return dict(zip(concepts, results))


def track_objects(
    clip: Path, concepts: list[str], fps: float | None, file_id: str | None = None
) -> tuple[dict[str, Track | None], float]:
    """Track every concept on the hosted SAM API, uploading the clip if needed.

    Returns ``{concept: Track or None}`` and the wall-clock seconds taken.
    """
    start = time.perf_counter()
    tracks = asyncio.run(_track_all(clip, concepts, fps, file_id))
    return tracks, round(time.perf_counter() - start, 2)
