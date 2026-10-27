"""Probe 1: can SAM 3.1 track named objects across a short video?

Usage:  python probes/p1_sam_video.py CLIP.mp4 --concept "red block" --concept "bowl"

Request shape and stream handling follow Meta's MIT-licensed cookbook recipe
07_sam/01_api_basics/python/sam_api.py (credited in the README).
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncIterable, AsyncIterator

from meta_sam_parser import parse_responses_stream, video_segmentation_format
from openai import AsyncOpenAI

from common import META_BASE_URL, Timer, error_text, require_env, save, small_mp4, video_info

MODEL = "sam-3.1"


async def finalized_events(stream: AsyncIterable[Any]) -> AsyncIterator[dict[str, Any]]:
    """The API sends response.content_part.done but not response.output_text.done, which the
    parser needs to close a text lane, so synthesize the latter (same fix as the cookbook)."""
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


async def track(api: AsyncOpenAI, file_id: str, concept: str) -> dict[str, Any]:
    with Timer() as timer:
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
        async with stream, aclosing(finalized_events(stream)) as events:
            parsed = parse_responses_stream(events, video_segmentation_format())
            async with parsed:
                async for _ in parsed:
                    pass
                result = await parsed.final_result()

    boxes = [r for r in result.records if r.kind == "box"]
    frames_by_object: dict[str, set[int]] = defaultdict(set)
    for record in result.records:
        if record.kind in ("box", "mask") and record.frame is not None:
            frames_by_object[record.object_id].add(record.frame.frame_index)
    return {
        "concept": concept,
        "seconds": timer.seconds,
        "outcome": result.outcome.status,
        "records": len(result.records),
        "errors": [d.message for d in result.diagnostics if d.severity == "error"],
        "objects": {
            oid: {"first_frame": min(f), "last_frame": max(f), "visible_frames": len(f)}
            for oid, f in sorted(frames_by_object.items())
        },
        "max_frame_index": max((max(f) for f in frames_by_object.values()), default=None),
        # One box per object per frame: the trajectory the task-graph step will consume.
        "boxes": [
            {"object_id": b.object_id, "frame": b.frame.frame_index if b.frame else None,
             "box": [b.left, b.top, b.right, b.bottom], "confidence": b.confidence}
            for b in boxes
        ],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("clip", type=Path)
    parser.add_argument("--concept", action="append", required=True, help="short noun phrase; repeatable")
    args = parser.parse_args()

    clip = small_mp4(args.clip)
    report: dict[str, Any] = {"probe": "sam-3.1 video tracking", "source": str(args.clip),
                              "uploaded": video_info(clip), "results": []}
    async with AsyncOpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY")) as api:
        try:
            with Timer() as timer:
                uploaded = await api.files.create(
                    file=(clip.name, clip.read_bytes(), "video/mp4"), purpose="user_data"
                )
            report["file_id"], report["upload_seconds"] = uploaded.id, timer.seconds
            print(f"uploaded {clip.name} as {uploaded.id} in {timer.seconds}s")
        except Exception as exc:  # noqa: BLE001 - a probe reports every failure
            report["upload_error"] = error_text(exc)
            print("UPLOAD FAILED:", report["upload_error"])
            print("saved", save("p1-sam", report))
            return

        for concept in args.concept:
            try:
                result = await track(api, uploaded.id, concept)
                report["results"].append(result)
                print(f"[{concept}] {result['outcome']} in {result['seconds']}s: "
                      f"{len(result['objects'])} object(s), max frame {result['max_frame_index']}, "
                      f"{len(result['boxes'])} boxes, errors={result['errors']}")
            except Exception as exc:  # noqa: BLE001
                report["results"].append({"concept": concept, "error": error_text(exc)})
                print(f"[{concept}] FAILED: {error_text(exc)}")

    frames = report["uploaded"]["frames"]
    print(f"video has {frames} frames at {report['uploaded']['fps']} fps "
          "(compare with max frame index above to learn SAM's sampling rate)")
    print("saved", save("p1-sam", report))


if __name__ == "__main__":
    asyncio.run(main())
