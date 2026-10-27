"""Geometric checks for the spatial relations a task graph asserts.

These judge the *achieved* scene, so they are what turns "the robot moved things" into
"the robot accomplished the task." A relation the model names but the scene does not
satisfy is reported as not-holding, never assumed.
"""

from __future__ import annotations

import numpy as np

from .scene import Scene

NEAR_XY = 0.06        # m, horizontal tolerance for "on" / "near"
REST_TOL = 0.03       # m, how close to the table counts as "resting"


def holds(relation: str, a: str, b: str, scene: Scene) -> bool:
    if relation == "on":
        return _on(a, b, scene)
    if relation == "inside":
        return _inside(a, b, scene)
    if relation == "near":
        return float(np.linalg.norm(scene.xy(a) - scene.xy(b))) < 3 * NEAR_XY
    return False


def _on(a: str, b: str, scene: Scene) -> bool:
    """a rests on top of b: aligned in x/y and one block-height above b."""
    if not (scene.is_block(a) and scene.is_block(b)):
        return False
    pa, pb = scene.pos(a), scene.pos(b)
    aligned = float(np.linalg.norm(pa[:2] - pb[:2])) < NEAR_XY
    stacked = abs((pa[2] - pb[2]) - 2 * scene.block_half) < REST_TOL
    return aligned and stacked


def _inside(a: str, b: str, scene: Scene) -> bool:
    """a sits within tray region b's footprint and rests on the table (not carried)."""
    if not scene.is_block(a) or b not in scene.trays:
        return False
    pa = scene.pos(a)
    cx, cy = scene.trays[b]
    hx, hy = scene.tray_half
    within = abs(pa[0] - cx) <= hx and abs(pa[1] - cy) <= hy
    resting = abs(pa[2] - (scene.table_top + scene.block_half)) < REST_TOL
    return within and resting
