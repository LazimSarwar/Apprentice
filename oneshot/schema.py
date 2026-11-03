"""The task-graph data contract.

`RawGraph` is exactly what Muse Spark is asked to return (objects, steps, final
relations). `TaskGraph` is that graph after SAM evidence is linked in: every object
carries its track, and every step carries the trajectory samples that back it.
Keeping the two apart means a model that hallucinates a step still can't invent the
evidence for it — the evidence only ever comes from the tracker.
"""

from __future__ import annotations

from typing import Literal, Optional
import math

from pydantic import BaseModel, Field, model_validator

ACTIONS = ("pick", "place", "stack", "push")
RELATIONS = ("on", "inside", "near")

Action = Literal["pick", "place", "stack", "push"]
RelationKind = Literal["on", "inside", "near"]


# --- what the model returns ------------------------------------------------

class Object(BaseModel):
    """A physical object the demonstration manipulates."""

    id: str = Field(description="stable snake_case handle, e.g. 'red_block'")
    color: Optional[str] = None
    shape: Optional[str] = None
    kind: Optional[Literal["solid", "container", "surface"]] = None
    tracking_description: Optional[str] = Field(default=None, max_length=160,
        description="Short visible object description for tracking, without actions or expected locations")

    def concept(self) -> str:
        """Short noun phrase to hand the tracker, e.g. 'red block'."""
        if self.tracking_description and self.tracking_description.strip():
            return " ".join(self.tracking_description.split())
        words = [w for w in (self.color, self.shape) if w]
        return " ".join(words) if words else self.id.replace("_", " ")

    def reference_hint(self) -> dict:
        """Appearance only: never leak expected actions or positions into detection."""
        return self.model_dump(include={"id", "color", "shape", "kind", "tracking_description"}, exclude_none=True)


class Step(BaseModel):
    """One ordered action in the task."""

    order: int
    start_s: float
    end_s: float
    action: Action
    object: str = Field(description="id of the object being moved")
    target: Optional[str] = Field(default=None, description="id of the object acted upon, or null")
    relation: Optional[RelationKind] = Field(
        default=None, description="spatial result of the step, or null"
    )


class Relation(BaseModel):
    """A spatial relation that holds once the task is done."""

    relation: RelationKind
    a: str
    b: str


class RawGraph(BaseModel):
    """The unverified graph straight from the model."""

    objects: list[Object] = Field(max_length=16)
    steps: list[Step] = Field(max_length=128)
    final_relations: list[Relation] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def coherent_graph(self):
        ids = [o.id for o in self.objects]
        if any(not oid.strip() for oid in ids) or len(set(ids)) != len(ids):
            raise ValueError("object ids must be nonempty and unique")
        orders = [s.order for s in self.steps]
        if len(set(orders)) != len(orders):
            raise ValueError("step orders must be unique")
        for s in self.steps:
            if s.object not in ids or (s.target is not None and s.target not in ids):
                raise ValueError("every step object and target must be declared in objects")
            if s.target == s.object:
                raise ValueError("an object cannot be its own placement target")
            if s.order < 1 or not (0 <= s.start_s < s.end_s < float("inf")):
                raise ValueError("steps require positive order and finite, increasing nonnegative timestamps")
        for r in self.final_relations:
            if r.a not in ids or r.b not in ids:
                raise ValueError("every final relation must refer to declared objects")
            if r.a == r.b:
                raise ValueError("a placement relation must connect distinct objects")
        return self


# --- what SAM contributes --------------------------------------------------

class Box(BaseModel):
    """One tracked bounding box, with the time it was seen."""

    frame: int
    t_s: float
    box: list[float] = Field(description="[left, top, right, bottom] in pixels")
    confidence: Optional[float] = None

    @model_validator(mode="after")
    def valid_box(self):
        if (self.frame < 0 or not math.isfinite(self.t_s) or self.t_s < 0 or len(self.box) != 4
                or not all(math.isfinite(v) and v >= 0 for v in self.box)
                or self.box[0] >= self.box[2] or self.box[1] >= self.box[3]):
            raise ValueError("boxes require finite ordered coordinates and nonnegative frame/time")
        if self.confidence is not None and not (0 <= self.confidence <= 1):
            raise ValueError("confidence must be between zero and one")
        return self


class Track(BaseModel):
    """A SAM trajectory for one object across the clip."""

    concept: str
    sam_object_id: str
    first_s: float
    last_s: float
    visible_frames: int
    boxes: list[Box] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered_track(self):
        if not (0 <= self.first_s <= self.last_s < float("inf")) or self.visible_frames < 0:
            raise ValueError("track summary times and count must be finite and nonnegative")
        if any(a.t_s > b.t_s or a.frame > b.frame for a, b in zip(self.boxes, self.boxes[1:])):
            raise ValueError("track samples must be ordered by frame and timestamp")
        return self

    def in_window(self, start_s: float, end_s: float) -> list[Box]:
        return [b for b in self.boxes if start_s <= b.t_s <= end_s]


# --- the linked result -----------------------------------------------------

class VideoInfo(BaseModel):
    width: int
    height: int
    fps: Optional[float] = None
    frames: Optional[int] = None
    duration_s: float

    @model_validator(mode="after")
    def valid_video(self):
        if self.width <= 0 or self.height <= 0 or not (0 < self.duration_s < float("inf")):
            raise ValueError("video dimensions and finite duration must be positive")
        if self.fps is not None and not (0 < self.fps < float("inf")):
            raise ValueError("video fps must be positive and finite")
        if self.frames is not None and self.frames < 1:
            raise ValueError("frame count must be positive")
        return self


class StepEvidence(BaseModel):
    """Trajectory samples that back a step, pulled from the object's track."""

    object_boxes_in_window: int = 0
    target_boxes_in_window: int = 0
    samples: list[Box] = Field(default_factory=list)


class ObjectWithEvidence(Object):
    from_reference: bool = False   # The reading left it out; it was searched for from the reference.
    tracked: bool = False
    track: Optional[Track] = None


class StepWithEvidence(Step):
    evidence: StepEvidence = Field(default_factory=StepEvidence)


class GraphMeta(BaseModel):
    provider: str = "hosted"
    model: str
    extract_seconds: Optional[float] = None
    extract_cost_usd: Optional[float] = None
    track_seconds: Optional[float] = None
    tracking_errors: dict[str, str] = Field(default_factory=dict)
    tracking_parser: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class TaskGraph(BaseModel):
    """The P0 deliverable: an ordered, evidence-linked plan the executor can run."""

    source: str
    video: VideoInfo
    objects: list[ObjectWithEvidence]
    steps: list[StepWithEvidence]
    final_relations: list[Relation] = Field(default_factory=list)
    meta: GraphMeta

    @model_validator(mode="after")
    def coherent_graph(self):
        RawGraph.coherent_graph(self)
        if len(self.objects) > 16 or len(self.steps) > 128 or len(self.final_relations) > 128:
            raise ValueError("task graphs support at most 16 objects and 128 steps/relations")
        # Frame-based timestamps can land one sample beyond a rounded duration.
        tolerance = max(0.05, 1 / (self.video.fps or 30))
        if any(s.end_s > self.video.duration_s + tolerance for s in self.steps):
            raise ValueError("step timestamps must fall within the video")
        for obj in self.objects:
            if obj.track:
                for box in obj.track.boxes:
                    if box.t_s > self.video.duration_s + tolerance:
                        raise ValueError("tracking timestamps must fall within the video")
                    if box.box[2] > self.video.width or box.box[3] > self.video.height:
                        raise ValueError("tracking box must lie within the video dimensions")
        return self
