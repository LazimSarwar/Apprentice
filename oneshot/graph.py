"""P0 orchestrator: video in, evidence-linked task graph out.

1. shrink the clip and upload it once (Spark and SAM share the file, so they see
   identical frames and frame indices map to the same timestamps);
2. Muse Spark reads the task graph;
3. SAM tracks each named object;
4. link every object to its track and every step to the trajectory in its time window.
"""

from __future__ import annotations

from pathlib import Path
import time

from .clients import ROOT
from .evidence import TRACK_TIMEOUT_SECONDS, TrackingBatch, track_objects
from .extract import extract_graph
from .media import small_mp4, video_info
from .schema import (
    GraphMeta,
    ObjectWithEvidence,
    StepEvidence,
    StepWithEvidence,
    TaskGraph,
)

MAX_SAMPLES = 8


def _downsample(boxes: list, limit: int = MAX_SAMPLES) -> list:
    """Evenly spaced subset so a step's evidence stays readable."""
    if len(boxes) <= limit:
        return boxes
    step = len(boxes) / limit
    return [boxes[int(i * step)] for i in range(limit)]


def track_with_fallback(clip: Path, objects: list, fps: float | None,
                        file_id: str | None = None) -> TrackingBatch:
    """SAM track per object id. An object its usual concept ("blue sheet") finds nothing for
    is retried once with a plainer one ("blue rectangle") — only when its colour is unique
    among the objects, so the retry can't latch onto a different object."""
    concepts = {o.id: o.concept() for o in objects}
    # A text concept may find several identical parts. The longest trajectory cannot
    # identify which graph object it belongs to. Abstain until instance grounding exists.
    normalized = [c.casefold() for c in concepts.values()]
    ambiguous = {oid for oid, concept in concepts.items() if normalized.count(concept.casefold()) > 1}
    unique = {oid: c for oid, c in concepts.items() if oid not in ambiguous}
    start = time.monotonic()
    batch = track_objects(clip, sorted(set(unique.values())), fps, file_id=file_id)
    by_id = {oid: batch.tracks.get(c) for oid, c in concepts.items()}
    errors = {oid: batch.errors[c] for oid, c in concepts.items() if c in batch.errors}
    errors.update({oid: "Ambiguous object identity: multiple objects have the same visual description." for oid in ambiguous})
    colours = [o.color for o in objects if o.color]
    missing = {o.id: f"{o.color} rectangle" for o in objects
               if by_id[o.id] is None and o.id not in errors and o.color and colours.count(o.color) == 1}
    remaining = TRACK_TIMEOUT_SECONDS - (time.monotonic() - start)
    if missing and remaining > 0:
        more = track_objects(clip, sorted(set(missing.values())), fps, file_id=batch.file_id,
                             timeout_seconds=remaining)
        for oid, concept in missing.items():
            by_id[oid] = more.tracks.get(concept)
            if concept in more.errors:
                errors[oid] = more.errors[concept]
    return TrackingBatch(tracks=by_id, errors=errors, seconds=round(time.monotonic() - start, 2),
                         file_id=batch.file_id)


def build_task_graph(video: Path, work_dir: Path | None = None, provider: str = "hosted",
                     known_objects: list[dict] | None = None, track: bool = True,
                     spoken: str | None = None, effort: str = "low") -> TaskGraph:
    """Build the evidence-linked task graph for a demonstration video.

    `provider` selects the extractor: "hosted" (Muse Spark 1.3, native video) or
    "baseten" / "fireworks" (Muse Spark Open, open weights; video / keyframes). SAM tracking is unchanged.
    `known_objects` guides the extractor to reuse a reference video's object ids;
    `track=False` skips SAM (faster, but the graph carries no evidence); `spoken` is the
    reference's narration, used to name objects in the person's words; `effort` is the hosted
    model's reasoning effort.
    """
    video = Path(video)
    work_dir = work_dir or (ROOT / "runs")
    clip = small_mp4(video, work_dir / "clips")
    info = video_info(clip)

    raw, meta = extract_graph(clip, provider=provider, known_objects=known_objects, spoken=spoken, effort=effort)

    concepts = {obj.id: obj.concept() for obj in raw.objects}
    if track:
        tracking = track_with_fallback(clip, raw.objects, info.fps, file_id=meta.get("file_id"))
        tracks_by_id, track_seconds = tracking.tracks, tracking.seconds
    else:
        tracking = TrackingBatch()
        tracks_by_id, track_seconds = {}, None

    warnings: list[str] = []

    objects: list[ObjectWithEvidence] = []
    track_by_object = {}
    for obj in raw.objects:
        obj_track = tracks_by_id.get(obj.id)
        track_by_object[obj.id] = obj_track
        if obj_track is None and track:
            warnings.append(f"no SAM track for object '{obj.id}' ({concepts[obj.id]!r})")
        objects.append(
            ObjectWithEvidence(
                **obj.model_dump(),
                tracked=obj_track is not None, track=obj_track,
            )
        )

    steps: list[StepWithEvidence] = []
    for step in sorted(raw.steps, key=lambda s: s.order):
        obj_track = track_by_object.get(step.object)
        tgt_track = track_by_object.get(step.target) if step.target else None
        obj_in = obj_track.in_window(step.start_s, step.end_s) if obj_track else []
        tgt_in = tgt_track.in_window(step.start_s, step.end_s) if tgt_track else []
        if obj_track is not None and not obj_in:
            warnings.append(
                f"step {step.order} ({step.action} {step.object}): "
                f"no track boxes in [{step.start_s}, {step.end_s}]s"
            )
        steps.append(
            StepWithEvidence(
                **step.model_dump(),
                evidence=StepEvidence(
                    object_boxes_in_window=len(obj_in),
                    target_boxes_in_window=len(tgt_in),
                    samples=_downsample(obj_in),
                ),
            )
        )

    return TaskGraph(
        source=str(video),
        video=info,
        objects=objects,
        steps=steps,
        final_relations=raw.final_relations,
        meta=GraphMeta(
            provider=meta["provider"],
            model=meta["model"],
            extract_seconds=meta["extract_seconds"],
            extract_cost_usd=meta["extract_cost_usd"],
            track_seconds=track_seconds,
            warnings=warnings,
            tracking_errors=tracking.errors,
            tracking_parser="sam_boxes_v1" if track else None,
        ),
    )


def add_evidence(graph: TaskGraph, clip: Path) -> TaskGraph:
    """Attach SAM tracks to a graph that was built without tracking (e.g. `track=False`),
    so the same extraction can later be shown with its evidence."""
    info = video_info(clip)
    tracking = track_with_fallback(clip, graph.objects, info.fps)
    tracks = tracking.tracks
    objects = [
        ObjectWithEvidence(**o.model_dump(exclude={"tracked", "track"}),
                           tracked=tracks.get(o.id) is not None, track=tracks.get(o.id))
        for o in graph.objects
    ]
    return graph.model_copy(update={
        "objects": objects,
        "meta": graph.meta.model_copy(update={"track_seconds": tracking.seconds,
            "tracking_errors": tracking.errors, "tracking_parser": "sam_boxes_v1"}),
    })
