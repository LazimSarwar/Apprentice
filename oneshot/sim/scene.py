"""Build a MuJoCo tabletop scene from a task graph's objects.

A Franka Panda (Menagerie, via `robot_descriptions`) stands at the origin in front of a
table. Each manipulable object becomes a free-floating block; each container (tray / bin /
bowl / plate) becomes a flat target region on the table. The scene is assembled
programmatically with `MjSpec` so it adapts to whatever objects the demonstration named.

Coordinates: metres, Panda base at the world origin, table top at ``TABLE_TOP``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import mujoco
import numpy as np
from robot_descriptions import panda_mj_description

from ..schema import Object

# Scene geometry (metres).
TABLE_CENTER = (0.5, 0.0)
TABLE_HALF = (0.35, 0.5, 0.05)
TABLE_TOP = 2 * TABLE_HALF[2]          # 0.10: table box sits on the floor
BLOCK_HALF = 0.025
BLOCK_REST_Z = TABLE_TOP + BLOCK_HALF
TRAY_HALF = (0.10, 0.10)

# Panda "home" pose (from the Menagerie keyframe): 7 arm joints, then two fingers.
HOME_ARM = (0.0, 0.0, 0.0, -1.571, 0.0, 1.571, -0.785)
FINGER_OPEN = 0.04

_CONTAINER_WORDS = ("tray", "bin", "bowl", "plate", "container", "box")
_BLOCK_COLORS = {
    "red": (0.85, 0.2, 0.2, 1.0),
    "blue": (0.2, 0.35, 0.85, 1.0),
    "green": (0.2, 0.7, 0.3, 1.0),
    "yellow": (0.9, 0.8, 0.2, 1.0),
    "purple": (0.5, 0.2, 0.6, 1.0),
    "orange": (0.9, 0.5, 0.15, 1.0),
    "brown": (0.5, 0.35, 0.2, 1.0),
    "beige": (0.82, 0.72, 0.52, 1.0),
    "pink": (0.9, 0.5, 0.65, 1.0),
    "maroon": (0.5, 0.12, 0.2, 1.0),
    "gray": (0.6, 0.6, 0.6, 1.0),
    "grey": (0.6, 0.6, 0.6, 1.0),
}


def _is_container(obj: Object) -> bool:
    text = f"{obj.id} {obj.shape or ''}".lower()
    return any(word in text for word in _CONTAINER_WORDS)


def _rgba(color: str | None, default=(0.5, 0.5, 0.5, 1.0)) -> tuple:
    return _BLOCK_COLORS.get((color or "").lower(), default)


@dataclass
class Scene:
    """A compiled scene plus the handles the executor and checker need."""

    model: mujoco.MjModel
    data: mujoco.MjData
    blocks: dict[str, int] = field(default_factory=dict)       # id -> body id
    block_qadr: dict[str, int] = field(default_factory=dict)   # id -> qpos address
    trays: dict[str, tuple[float, float]] = field(default_factory=dict)  # id -> (x, y)
    hand_id: int = -1
    table_top: float = TABLE_TOP
    block_half: float = BLOCK_HALF
    tray_half: tuple[float, float] = TRAY_HALF

    def xy(self, obj_id: str) -> np.ndarray:
        if obj_id in self.blocks:
            return self.data.xpos[self.blocks[obj_id]][:2].copy()
        return np.array(self.trays[obj_id])

    def pos(self, obj_id: str) -> np.ndarray:
        if obj_id in self.blocks:
            return self.data.xpos[self.blocks[obj_id]].copy()
        x, y = self.trays[obj_id]
        return np.array([x, y, self.table_top])

    def is_block(self, obj_id: str) -> bool:
        return obj_id in self.blocks


def _default_layout(blocks: list[Object], trays: list[Object]) -> dict[str, tuple[float, float]]:
    """Spread blocks across the front of the table and place trays toward the back."""
    places: dict[str, tuple[float, float]] = {}
    n = len(blocks)
    for i, b in enumerate(blocks):
        y = 0.0 if n == 1 else -0.2 + 0.4 * i / (n - 1)
        places[b.id] = (0.42, round(y, 3))
    m = len(trays)
    half = 0.15 if m <= 2 else 0.11 * (m - 1)       # keep 0.2 m tray regions from overlapping
    for j, t in enumerate(trays):
        y = 0.0 if m == 1 else -half + 2 * half * j / (m - 1)
        places[t.id] = (0.62, round(y, 3))
    return places


RENDER_W, RENDER_H = 960, 540


def _style(spec: mujoco.MjSpec) -> None:
    """Visual-only environment: gradient sky, light floor, key light, larger offscreen
    buffer. The floor does not collide, so physics is identical to the unstyled scene."""
    spec.visual.global_.offwidth = RENDER_W
    spec.visual.global_.offheight = RENDER_H
    spec.visual.headlight.ambient = [0.36, 0.36, 0.38]
    spec.visual.headlight.diffuse = [0.38, 0.38, 0.38]
    spec.visual.headlight.specular = [0.05, 0.05, 0.05]

    spec.add_texture(name="sky", type=mujoco.mjtTexture.mjTEXTURE_SKYBOX,
                     builtin=mujoco.mjtBuiltin.mjBUILTIN_GRADIENT,
                     rgb1=[0.96, 0.97, 0.99], rgb2=[0.78, 0.83, 0.90], width=512, height=3072)
    spec.add_texture(name="floor", type=mujoco.mjtTexture.mjTEXTURE_2D,
                     builtin=mujoco.mjtBuiltin.mjBUILTIN_CHECKER,
                     rgb1=[0.88, 0.89, 0.91], rgb2=[0.86, 0.87, 0.89], width=256, height=256)
    floor = spec.add_material(name="floor", texrepeat=[8, 8], reflectance=0.05)
    floor.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = "floor"

    world = spec.worldbody
    world.add_geom(name="floor", type=mujoco.mjtGeom.mjGEOM_PLANE, size=[4, 4, 0.05],
                   material="floor", contype=0, conaffinity=0)
    world.add_light(name="key", pos=[0.3, -0.8, 2.0], dir=[0.1, 0.4, -1.0],
                    diffuse=[0.42, 0.42, 0.42], castshadow=True)


def build_scene(objects: Iterable[Object], layout: dict[str, tuple[float, float]] | None = None) -> Scene:
    """Compile a scene holding every object the task graph named."""
    objects = list(objects)
    blocks = [o for o in objects if not _is_container(o)]
    trays = [o for o in objects if _is_container(o)]
    places = layout or _default_layout(blocks, trays)

    spec = mujoco.MjSpec.from_file(panda_mj_description.MJCF_PATH)
    spec.option.timestep = 0.002
    for key in list(spec.keys):          # drop the 9-dof home key; freejoints change nq
        spec.delete(key)
    _style(spec)

    world = spec.worldbody
    world.add_geom(
        name="table", type=mujoco.mjtGeom.mjGEOM_BOX,
        pos=[*TABLE_CENTER, TABLE_HALF[2]], size=list(TABLE_HALF),
        rgba=[0.72, 0.58, 0.42, 1.0],
    )
    for tray in trays:
        x, y = places[tray.id]
        world.add_geom(
            name=f"region_{tray.id}", type=mujoco.mjtGeom.mjGEOM_BOX,
            pos=[x, y, TABLE_TOP + 0.001], size=[*TRAY_HALF, 0.001],
            rgba=[*_rgba(tray.color, (0.55, 0.55, 0.55, 1.0))[:3], 0.6],
            contype=0, conaffinity=0,        # a visual target region, not a collider
        )
    for block in blocks:
        x, y = places[block.id]
        body = world.add_body(name=block.id, pos=[x, y, BLOCK_REST_Z])
        body.add_freejoint()
        body.add_geom(
            type=mujoco.mjtGeom.mjGEOM_BOX, size=[BLOCK_HALF] * 3,
            rgba=list(_rgba(block.color)), mass=0.05,
        )

    model = spec.compile()
    data = mujoco.MjData(model)
    scene = Scene(
        model=model, data=data,
        hand_id=mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "hand"),
    )
    for block in blocks:
        bid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, block.id)
        scene.blocks[block.id] = bid
        jnt = model.body_jntadr[bid]
        scene.block_qadr[block.id] = model.jnt_qposadr[jnt]
    for tray in trays:
        scene.trays[tray.id] = places[tray.id]

    reset_home(scene)
    return scene


def reset_home(scene: Scene) -> None:
    """Put the arm at home with fingers open; mj_resetData restores blocks to qpos0."""
    m, d = scene.model, scene.data
    mujoco.mj_resetData(m, d)                 # blocks return to their compiled start pose
    d.qpos[:7] = HOME_ARM
    d.qpos[7:9] = FINGER_OPEN
    d.ctrl[:7] = HOME_ARM                     # position actuators hold the home pose
    d.ctrl[7] = 255.0                         # gripper open
    mujoco.mj_forward(m, d)
