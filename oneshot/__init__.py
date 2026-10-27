"""OneShot: turn a demonstration video into an evidence-linked task graph.

P0 of the pipeline. `build_task_graph(video)` runs Muse Spark 1.3 to read the task
(objects, ordered steps, final relations) and SAM 3.1 to track each object across the
clip, then links every step to the tracked motion that backs it. The result is the
input contract the simulated executor (P1) consumes.
"""

from __future__ import annotations

from .graph import build_task_graph
from .schema import Box, Object, Relation, Step, TaskGraph, Track

__all__ = [
    "build_task_graph",
    "TaskGraph",
    "Object",
    "Step",
    "Relation",
    "Track",
    "Box",
]
