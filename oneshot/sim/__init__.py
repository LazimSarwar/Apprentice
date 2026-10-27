"""P1: run an evidence-linked task graph on a simulated Franka Panda.

The MuJoCo scene, IK and pick-and-place phases adapt Meta's MIT-licensed cookbook
recipe `03_use_cases/14_muse_robotics` (Panda model from MuJoCo Menagerie, Apache-2.0,
pulled via `robot_descriptions`). OneShot's new work is running a *learned* task graph
(not a hand-written command), the `stack` and `push` primitives the recipe lacks,
geometric relation checking, and honest per-step outcomes.
"""

from __future__ import annotations

from .executor import Disturbance, ExecutionResult, execute
from .scene import Scene, build_scene

__all__ = ["execute", "ExecutionResult", "Disturbance", "build_scene", "Scene"]
