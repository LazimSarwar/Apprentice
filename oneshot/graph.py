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


def build_task_graph(video: Path, work_dir: Path | None = None,
                     provider: str = "hosted") -> TaskGraph:
    """Build the evidence-linked task graph for a demonstration video.

    `provider` selects the extractor: "hosted" (Muse Spark 1.3, native video) or
    "fireworks" (Muse Spark Open, open weights, keyframes). SAM tracking is unchanged.
    """
    video = Path(video)
    work_dir = work_dir or (ROOT / "runs")
    clip = small_mp4(video, work_dir / "clips")
    info = video_info(clip)

    raw, meta = extract_graph(clip, provider=provider)

    concepts = {obj.id: obj.concept() for obj in raw.objects}
    unique = sorted(set(concepts.values()))
    tracks_by_concept, track_seconds = track_objects(
        clip, unique, info.fps, file_id=meta.get("file_id")
    )

    warnings: list[str] = []

    objects: list[ObjectWithEvidence] = []
    track_by_object = {}
    for obj in raw.objects:
        track = tracks_by_concept.get(concepts[obj.id])
        track_by_object[obj.id] = track
        if track is None:
            warnings.append(f"no SAM track for object '{obj.id}' ({concepts[obj.id]!r})")
        objects.append(
            ObjectWithEvidence(
                id=obj.id, color=obj.color, shape=obj.shape,
                tracked=track is not None, track=track,
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
