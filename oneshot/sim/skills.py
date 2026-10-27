"""Deterministic manipulation primitives built on the Panda controller.

`pick_place` and `stack` lift, carry and release (prehensile). `push` slides a block
along the table without lifting, so the two kinds of motion stay distinguishable — the
same final arrangement can be reached by a lift or by a push, and later stages care which.
The pick-and-place phase sequence follows the cookbook's `_pick_and_place_at` (MIT).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .robot import HAND_OFFSET, Panda
from .scene import Scene

APPROACH_CLEAR = 0.12     # hand height above a grasp, before descending
LIFT_H = 0.22             # carried block height above the table
PUSH_HOVER = 0.10         # hand height above the table before a push descends
MAX_RESIDUAL = 0.05       # m; a move that ends farther than this from target failed


@dataclass
class SkillResult:
    ok: bool
    residual: float
    reason: str = ""


def _hand_z_for_block_center(scene: Scene, block_center_z: float) -> float:
    return block_center_z + HAND_OFFSET


def pick_place(robot: Panda, scene: Scene, block: str, target_xy, place_block_z: float) -> SkillResult:
    c = scene.pos(block)
    grasp_z = _hand_z_for_block_center(scene, scene.table_top + scene.block_half)
    lift_z = scene.table_top + LIFT_H + HAND_OFFSET
    place_hand_z = place_block_z + HAND_OFFSET
    tx, ty = float(target_xy[0]), float(target_xy[1])

    # Reach the block before grasping — never attach to a block the hand can't touch.
    robot.move_to([c[0], c[1], grasp_z + APPROACH_CLEAR])
    reach = robot.move_to([c[0], c[1], grasp_z])
    if reach > MAX_RESIDUAL:
        return SkillResult(False, reach, f"cannot reach '{block}' to grasp (residual {reach:.3f} m)")

    robot.set_gripper(False)
    robot.attach(block, "lift")              # now holding: later moves carry the block continuously
    worst = max(robot.move_to([c[0], c[1], lift_z]),
                robot.move_to([tx, ty, lift_z]),
                robot.move_to([tx, ty, place_hand_z]))
    robot.detach()
    robot.set_gripper(True)
    robot.advance(150)                       # let the block settle
    robot.move_to([tx, ty, lift_z])          # retreat
    if worst > MAX_RESIDUAL:
        return SkillResult(False, worst, f"could not place at target (residual {worst:.3f} m)")
    return SkillResult(True, max(reach, worst))


def stack(robot: Panda, scene: Scene, block: str, onto: str) -> SkillResult:
    if not scene.is_block(onto):
        return SkillResult(False, 0.0, f"cannot stack on non-block '{onto}'")
    base = scene.pos(onto)
    place_block_z = base[2] + 2 * scene.block_half
    return pick_place(robot, scene, block, base[:2], place_block_z)


def place_in(robot: Panda, scene: Scene, block: str, target: str) -> SkillResult:
    xy = scene.xy(target)
    return pick_place(robot, scene, block, xy, scene.table_top + scene.block_half)


def push(robot: Panda, scene: Scene, block: str, target_xy) -> SkillResult:
    c = scene.pos(block)
    tx, ty = float(target_xy[0]), float(target_xy[1])
    push_z = _hand_z_for_block_center(scene, scene.table_top + scene.block_half)

    direction = np.array([tx, ty]) - c[:2]
    dist = float(np.linalg.norm(direction))
    if dist < 1e-3:
        return SkillResult(True, 0.0)        # already there
    direction /= dist
    behind = c[:2] - direction * (scene.block_half + 0.03)

    # Reach the contact point behind the block before attaching — no teleport-drag.
    robot.move_to([behind[0], behind[1], push_z + PUSH_HOVER])
    reach = robot.move_to([behind[0], behind[1], push_z])
    if reach > MAX_RESIDUAL:
        return SkillResult(False, reach, f"cannot reach behind '{block}' to push (residual {reach:.3f} m)")

    robot.attach(block, "slide")
    slid = robot.move_to([tx, ty, push_z], duration=1.5)
    robot.detach()
    robot.advance(120)
    robot.move_to([tx, ty, push_z + PUSH_HOVER])
    if slid > MAX_RESIDUAL:
        return SkillResult(False, slid, f"could not push to target (residual {slid:.3f} m)")
    return SkillResult(True, max(reach, slid))
