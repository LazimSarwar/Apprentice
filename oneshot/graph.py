"""P0 orchestrator: video in, evidence-linked task graph out.

1. shrink the clip and upload it once (Spark and SAM share the file, so they see
   identical frames and frame indices map to the same timestamps);
2. Muse Spark reads the task graph;
3. SAM tracks each named object;
4. link every object to its track and every step to the trajectory in its time window.
"""

from __future__ import annotations

from pathlib import Path

from .clients import ROOT
from .evidence import track_objects
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


def build_task_graph(video: Path, work_dir: Path | None = None, provider: str = "hosted",
                     known_objects: list[dict] | None = None, track: bool = True) -> TaskGraph:
    """Build the evidence-linked task graph for a demonstration video.

    `provider` selects the extractor: "hosted" (Muse Spark 1.3, native video) or
    "fireworks" (Muse Spark Open, open weights, keyframes). SAM tracking is unchanged.
    `known_objects` guides the extractor to reuse a reference video's object ids;
    `track=False` skips SAM (faster, but the graph carries no evidence).
    """
    video = Path(video)
    work_dir = work_dir or (ROOT / "runs")
    clip = small_mp4(video, work_dir / "clips")
    info = video_info(clip)

    raw, meta = extract_graph(clip, provider=provider, known_objects=known_objects)

    concepts = {obj.id: obj.concept() for obj in raw.objects}
    unique = sorted(set(concepts.values()))
    if track:
        tracks_by_concept, track_seconds = track_objects(
            clip, unique, info.fps, file_id=meta.get("file_id")
        )
    else:
        tracks_by_concept, track_seconds = {}, None

    warnings: list[str] = []

    objects: list[ObjectWithEvidence] = []
    track_by_object = {}
    for obj in raw.objects:
        obj_track = tracks_by_concept.get(concepts[obj.id])
        track_by_object[obj.id] = obj_track
        if obj_track is None and track:
            warnings.append(f"no SAM track for object '{obj.id}' ({concepts[obj.id]!r})")
        objects.append(
            ObjectWithEvidence(
                id=obj.id, color=obj.color, shape=obj.shape,
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
        ),
    )


def add_evidence(graph: TaskGraph, clip: Path) -> TaskGraph:
    """Attach SAM tracks to a graph that was built without tracking (e.g. `track=False`),
    so the same extraction can later be shown with its evidence."""
    info = video_info(clip)
    concepts = {o.id: o.concept() for o in graph.objects}
    tracks, seconds = track_objects(clip, sorted(set(concepts.values())), info.fps)
    objects = [
        ObjectWithEvidence(id=o.id, color=o.color, shape=o.shape,
                           tracked=tracks.get(concepts[o.id]) is not None,
                           track=tracks.get(concepts[o.id]))
        for o in graph.objects
    ]
    return graph.model_copy(update={
        "objects": objects,
        "meta": graph.meta.model_copy(update={"track_seconds": seconds}),
    })
