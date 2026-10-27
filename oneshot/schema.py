"""The task-graph data contract.

`RawGraph` is exactly what Muse Spark is asked to return (objects, steps, final
relations). `TaskGraph` is that graph after SAM evidence is linked in: every object
carries its track, and every step carries the trajectory samples that back it.
Keeping the two apart means a model that hallucinates a step still can't invent the
evidence for it — the evidence only ever comes from the tracker.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

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

    def concept(self) -> str:
        """Short noun phrase to hand the tracker, e.g. 'red block'."""
        words = [w for w in (self.color, self.shape) if w]
        return " ".join(words) if words else self.id.replace("_", " ")


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

    objects: list[Object]
    steps: list[Step]
    final_relations: list[Relation] = Field(default_factory=list)


# --- what SAM contributes --------------------------------------------------

class Box(BaseModel):
    """One tracked bounding box, with the time it was seen."""

    frame: int
    t_s: float
    box: list[float] = Field(description="[left, top, right, bottom] in pixels")
    confidence: Optional[float] = None


class Track(BaseModel):
    """A SAM trajectory for one object across the clip."""

    concept: str
    sam_object_id: str
    first_s: float
    last_s: float
    visible_frames: int
    boxes: list[Box] = Field(default_factory=list)

    def in_window(self, start_s: float, end_s: float) -> list[Box]:
        return [b for b in self.boxes if start_s <= b.t_s <= end_s]


# --- the linked result -----------------------------------------------------

class VideoInfo(BaseModel):
    width: int
    height: int
    fps: Optional[float] = None
    frames: Optional[int] = None
    duration_s: float


class StepEvidence(BaseModel):
    """Trajectory samples that back a step, pulled from the object's track."""

    object_boxes_in_window: int = 0
    target_boxes_in_window: int = 0
    samples: list[Box] = Field(default_factory=list)


class ObjectWithEvidence(Object):
    tracked: bool = False
    track: Optional[Track] = None


class StepWithEvidence(Step):
    evidence: StepEvidence = Field(default_factory=StepEvidence)


class GraphMeta(BaseModel):
    model: str
    extract_seconds: Optional[float] = None
    extract_cost_usd: Optional[float] = None
    track_seconds: Optional[float] = None
    warnings: list[str] = Field(default_factory=list)


class TaskGraph(BaseModel):
    """The P0 deliverable: an ordered, evidence-linked plan the executor can run."""

    source: str
    video: VideoInfo
    objects: list[ObjectWithEvidence]
    steps: list[StepWithEvidence]
    final_relations: list[Relation] = Field(default_factory=list)
    meta: GraphMeta
