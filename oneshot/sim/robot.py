"""Panda controller: damped-least-squares IK, smooth Cartesian moves, kinematic grasp.

The IK loop and the smoothstep move follow the cookbook's `robot.py` (MIT). Grasping is
simplified: instead of friction between the fingers, a held block is kinematically
carried with the hand — reliable, and disclosed as a simulation simplification. `lift`
mode carries the block below the hand; `slide` mode keeps it on the table (for pushes).
"""

from __future__ import annotations

import mujoco
import numpy as np

from .scene import RENDER_H, RENDER_W, Scene

ARM = slice(0, 7)
HAND_OFFSET = 0.10        # hand-body origin to carried block centre (m)
_DAMPING = 0.08
_MAX_DQ = 0.3


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


class Panda:
    def __init__(self, scene: Scene, record: bool = False, frame_skip: int = 16):
        self.scene = scene
        self.m, self.d = scene.model, scene.data
        self.hand = scene.hand_id
        self.down_quat = self.d.xquat[self.hand].copy()   # home hand points down
        self.arm_dofs = [self.m.jnt_dofadr[j] for j in range(7)]
        self._grasp: tuple[str, str] | None = None        # (block_id, mode)
        self.frames: list[np.ndarray] = []
        self._renderer = mujoco.Renderer(self.m, RENDER_H, RENDER_W) if record else None
        self._frame_skip = max(1, frame_skip)
        self._sim_steps = 0
        self._cam = mujoco.MjvCamera()
        self._cam.type = mujoco.mjtCamera.mjCAMERA_FREE
        self._cam.lookat[:] = [0.45, 0.0, 0.18]           # between the arm and the work area
        self._cam.distance = 1.35
        self._cam.azimuth = 145
        self._cam.elevation = -26

    # --- grasp ---------------------------------------------------------------
    def attach(self, block_id: str, mode: str = "lift") -> None:
        self._grasp = (block_id, mode)

    def detach(self) -> None:
        self._grasp = None

    def _hold(self) -> None:
        if self._grasp is None:
            return
        block_id, mode = self._grasp
        adr = self.scene.block_qadr[block_id]
        hand_pos = self.d.xpos[self.hand]
        if mode == "lift":
            pos = hand_pos + np.array([0.0, 0.0, -HAND_OFFSET])
        else:  # slide: track the hand in x/y, stay resting on the table
            pos = np.array([hand_pos[0], hand_pos[1], self.scene.table_top + self.scene.block_half])
        self.d.qpos[adr:adr + 3] = pos
        self.d.qpos[adr + 3:adr + 7] = [1.0, 0.0, 0.0, 0.0]
        dof = self.m.jnt_dofadr[self.m.body_jntadr[self.scene.blocks[block_id]]]
        self.d.qvel[dof:dof + 6] = 0.0

    # --- stepping ------------------------------------------------------------
    def advance(self, steps: int) -> None:
        for _ in range(steps):
            mujoco.mj_step(self.m, self.d)
            self._hold()
            self._sim_steps += 1
            if self._renderer is not None and self._sim_steps % self._frame_skip == 0:
                self._renderer.update_scene(self.d, self._cam)
                self.frames.append(self._renderer.render().copy())

    def set_gripper(self, open_: bool, steps: int = 120) -> None:
        self.d.ctrl[7] = 255.0 if open_ else 0.0
        self.advance(steps)

    # --- inverse kinematics --------------------------------------------------
    def _ik(self, target_pos: np.ndarray, iters: int = 300, tol: float = 2e-3) -> tuple[np.ndarray, float]:
        """Solve for arm joints placing the hand at target_pos, pointing down."""
        scratch = mujoco.MjData(self.m)
        scratch.qpos[:] = self.d.qpos
        jacp = np.zeros((3, self.m.nv))
        jacr = np.zeros((3, self.m.nv))
        err = np.zeros(6)
        quat_err = np.zeros(3)
        dq_full = np.zeros(self.m.nv)
        for _ in range(iters):
            mujoco.mj_forward(self.m, scratch)
            err[:3] = target_pos - scratch.xpos[self.hand]
            neg = np.zeros(4)
            res = np.zeros(4)
            mujoco.mju_negQuat(neg, scratch.xquat[self.hand])
            mujoco.mju_mulQuat(res, self.down_quat, neg)
            mujoco.mju_quat2Vel(quat_err, res, 1.0)
            err[3:] = quat_err
            if np.linalg.norm(err[:3]) < tol and np.linalg.norm(err[3:]) < 0.05:
                break
            mujoco.mj_jacBody(self.m, scratch, jacp, jacr, self.hand)
            J = np.vstack([jacp, jacr])[:, self.arm_dofs]        # 6 x 7
            dq = J.T @ np.linalg.solve(J @ J.T + _DAMPING**2 * np.eye(6), err)
            dq = np.clip(dq, -_MAX_DQ, _MAX_DQ)
            scratch.qpos[ARM] = np.clip(
                scratch.qpos[ARM] + dq,
                self.m.jnt_range[:7, 0], self.m.jnt_range[:7, 1],
            )
        return scratch.qpos[ARM].copy(), float(np.linalg.norm(err[:3]))

    def move_to(self, target_pos, duration: float = 1.0, settle: int = 80) -> float:
        """Drive the hand to target_pos with a smooth joint trajectory.

        Returns the residual Euclidean error between hand and target (m).
        """
        target_pos = np.asarray(target_pos, dtype=float)
        goal, _ = self._ik(target_pos)
        start = self.d.ctrl[ARM].copy()
        steps = max(1, int(duration / self.m.opt.timestep))
        for i in range(1, steps + 1):
            self.d.ctrl[ARM] = start + (goal - start) * _smoothstep(i / steps)
            self.advance(1)
        self.advance(settle)
        return float(np.linalg.norm(target_pos - self.d.xpos[self.hand]))
