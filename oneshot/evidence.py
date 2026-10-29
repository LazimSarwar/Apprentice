"""Step 2 of P0: track each object with SAM 3.1 to produce the evidence.

Adapted from the day-1 probe, whose request shape follows Meta's MIT-licensed cookbook
recipe 07_sam/01_api_basics (credited in the README). Tracks every object concept
concurrently, then keeps the longest-lived trajectory per concept as its track.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterable, AsyncIterator

# SAM 3.1 runs on the hosted API regardless of which model extracted the task graph.

from meta_sam_parser import parse_responses_stream, ResponsesStreamSourceError
from httpx2 import TransportError
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, RateLimitError, InternalServerError

from .clients import META_BASE_URL, require_env
from .media import frame_to_seconds
from .schema import Box, Track
from .box_parser import BoxVideoFormat

MODEL = "sam-3.1"
CONCURRENT = 3          # parallel tracking streams; more tends to get connections reset
TRACK_TIMEOUT_SECONDS = float(os.environ.get("TRACK_TIMEOUT_SECONDS", "120"))
STREAM_TIMEOUT_SECONDS = float(os.environ.get("TRACK_STREAM_TIMEOUT_SECONDS", "45"))


@dataclass
class TrackingBatch:
    tracks: dict[str, Track | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    seconds: float = 0.0
    file_id: str | None = None


class IncompleteTrackingError(RuntimeError):
    pass


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
        parsed = parse_responses_stream(events, BoxVideoFormat())
        async with parsed:
            result = await parsed.final_result()
    if result.outcome.status != "completed":
        raise IncompleteTrackingError("Tracking response ended before completion")

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


async def _track_retrying(api: AsyncOpenAI, file_id: str, concept: str, fps: float | None,
                          attempts: int = 2) -> Track | None:
    """Retry transient transport failures once; malformed output is not transient."""
    for i in range(attempts):
        try:
            return await asyncio.wait_for(_track_one(api, file_id, concept, fps), STREAM_TIMEOUT_SECONDS)
        except (TimeoutError, IncompleteTrackingError, APIConnectionError, APITimeoutError,
                RateLimitError, InternalServerError, ResponsesStreamSourceError) as exc:
            if isinstance(exc, ResponsesStreamSourceError) and (
                exc.operation != "next" or not isinstance(exc.cause,
                    (OSError, TimeoutError, APIConnectionError, APITimeoutError, TransportError))
            ):
                raise
            if i == attempts - 1:
                raise
            await asyncio.sleep(0.5 * (i + 1))
    return None


async def _track_all(
    clip: Path, concepts: list[str], fps: float | None, file_id: str | None, batch: TrackingBatch
) -> None:
    async with AsyncOpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY"),
                           timeout=30, max_retries=0) as api:
        if file_id is None:                      # extractor didn't upload (e.g. open-weights path)
            uploaded = await api.files.create(
                file=(clip.name, clip.read_bytes(), "video/mp4"), purpose="user_data"
            )
            file_id = uploaded.id
        batch.file_id = file_id
        gate = asyncio.Semaphore(CONCURRENT)

        async def one(concept: str) -> None:
            async with gate:
                started = time.perf_counter()
                try:
                    batch.tracks[concept] = await _track_retrying(api, file_id, concept, fps)
                    batch.completed.add(concept)
                except Exception as exc:
                    batch.errors[concept] = f"Tracking unavailable ({type(exc).__name__})"
                finally:
                    print(f"[tracking] {concept}: {time.perf_counter() - started:.1f}s "
                          f"{'complete' if concept in batch.completed else 'unavailable'}", flush=True)

        await asyncio.gather(*(one(c) for c in concepts))


def track_objects(
    clip: Path, concepts: list[str], fps: float | None, file_id: str | None = None,
    timeout_seconds: float | None = None,
) -> TrackingBatch:
    """Track every concept on the hosted SAM API, uploading the clip if needed.

    Preserves completed objects when another request fails. Errors and no detection
    are distinct: only a completed, empty response means no detection.
    """
    start = time.perf_counter()
    batch = TrackingBatch(tracks={c: None for c in concepts}, file_id=file_id)
    if not concepts:
        return batch
    async def bounded():
        try:
            await asyncio.wait_for(_track_all(clip, concepts, fps, file_id, batch),
                                   timeout=TRACK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds)
        except Exception as exc:
            for concept in concepts:
                if concept not in batch.completed and concept not in batch.errors:
                    batch.errors[concept] = f"Tracking unavailable ({type(exc).__name__})"

    asyncio.run(bounded())
    batch.seconds = round(time.perf_counter() - start, 2)
    return batch
