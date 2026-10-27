"""Run a task graph on the simulated Panda, recover from disturbances, report honestly.

Each step dispatches to a skill that reads the objects' *current* positions, so the arm
already adapts to a scene that has moved. On top of that, a monitor-and-repair pass runs
after the steps: it re-checks every relation the task graph said should hold and, for any
that doesn't, re-executes the step that establishes it from the current scene — the same
response whether a step slipped or something nudged an object mid-run. Everything is
reported with full denominators: steps, disturbances, repairs, and the final verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..schema import TaskGraph
from . import monitor, relations, skills
from .robot import Panda
from .scene import Scene, build_scene

MAX_REPAIR_ROUNDS = 3


@dataclass
class Disturbance:
    """An external shove applied to a block after a given step completes."""

    after_step: int
    object: str
    dxy: tuple[float, float]
    dz: float = 0.0


@dataclass
class StepOutcome:
    order: int
    action: str
    object: str
    target: str | None
    ok: bool
    residual: float
    reason: str = ""


@dataclass
class Repair:
    relation: str
    a: str
    b: str
    step_order: int
    round: int
    ok: bool


@dataclass
class RelationOutcome:
    relation: str
    a: str
    b: str
    holds: bool


@dataclass
class ExecutionResult:
    steps: list[StepOutcome] = field(default_factory=list)
    disturbances: list[str] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    final: list[RelationOutcome] = field(default_factory=list)
    procedure: list[monitor.RuleVerdict] = field(default_factory=list)
    completed: bool = False          # every step ran without the arm giving up
    success: bool = False            # completed AND all final relations hold
    reason: str = ""
    frames: list = field(default_factory=list)

    @property
    def corrections(self) -> int:
        return sum(r.ok for r in self.repairs)

    @property
    def procedure_ok(self) -> bool:
        return all(v.ok for v in self.procedure)

    def summary(self) -> str:
        done = sum(s.ok for s in self.steps)
        rel = sum(r.holds for r in self.final)
        verdict = "SUCCESS" if self.success else ("INCOMPLETE" if not self.completed else "WRONG FINAL STATE")
        lines = [f"{verdict}: {done}/{len(self.steps)} steps, {rel}/{len(self.final)} final relations hold"
                 + (f", {self.corrections} repair(s)" if self.repairs else "")]
        for s in self.steps:
            mark = "ok " if s.ok else "FAIL"
            tgt = f" -> {s.target}" if s.target else ""
            extra = f"  residual={s.residual:.3f}m" + (f"  ({s.reason})" if s.reason else "")
            lines.append(f"  [{mark}] {s.order}. {s.action} {s.object}{tgt}{extra}")
        for d in self.disturbances:
            lines.append(f"  [~~~ ] disturbance: {d}")
        for rp in self.repairs:
            lines.append(f"  [{'fix ' if rp.ok else 'FAIL'}] repair r{rp.round}: redo step {rp.step_order} "
                         f"to restore {rp.a} {rp.relation} {rp.b}")
        for r in self.final:
            lines.append(f"  [{'hold' if r.holds else 'MISS'}] {r.a} {r.relation} {r.b}")
        if self.procedure:
            lines.append(f"  procedure: {'FOLLOWED' if self.procedure_ok else 'VIOLATED'} "
                         f"({sum(v.ok for v in self.procedure)}/{len(self.procedure)} ordering rules)")
            for v in self.procedure:
                lines.append(f"    [{'ok  ' if v.ok else 'VIOL'}] {v.detail}")
        if self.reason:
            lines.append(f"  stopped: {self.reason}")
        return "\n".join(lines)


def _run_step(robot: Panda, scene: Scene, step) -> skills.SkillResult:
    action, obj, target = step.action, step.object, step.target
    if obj not in scene.blocks:
        return skills.SkillResult(False, 0.0, f"'{obj}' is not a manipulable block")
    if action == "stack":
        if not target:
            return skills.SkillResult(False, 0.0, "stack needs a target")
        return skills.stack(robot, scene, obj, target)
    if action == "push":
        if not target:
            return skills.SkillResult(False, 0.0, "push needs a target")
        return skills.push(robot, scene, obj, scene.xy(target))
    if action == "place":
        if not target:
            return skills.SkillResult(False, 0.0, "place needs a target")
        return skills.place_in(robot, scene, obj, target)
    if action == "pick":
        dest = scene.xy(target) if target else scene.xy(obj)
        return skills.pick_place(robot, scene, obj, dest, scene.table_top + scene.block_half)
    return skills.SkillResult(False, 0.0, f"unknown action '{action}'")


def _apply_disturbance(robot: Panda, scene: Scene, dist: Disturbance) -> str:
    """Shove a block, then let it settle — an external event the robot didn't cause."""
    adr = scene.block_qadr[dist.object]
    scene.data.qpos[adr] += dist.dxy[0]
    scene.data.qpos[adr + 1] += dist.dxy[1]
    scene.data.qpos[adr + 2] += dist.dz
    dof = scene.model.jnt_dofadr[scene.model.body_jntadr[scene.blocks[dist.object]]]
    scene.data.qvel[dof:dof + 6] = 0.0
    robot.advance(150)
    return f"{dist.object} shoved by ({dist.dxy[0]:+.2f}, {dist.dxy[1]:+.2f}) m after step {dist.after_step}"


def _establishing_step(graph: TaskGraph, rel) -> object | None:
    """The step that makes relation `rel` hold: its object is rel.a, ideally targeting rel.b."""
    candidates = [s for s in graph.steps if s.object == rel.a]
    exact = [s for s in candidates if s.target == rel.b]
    chosen = exact or candidates
    return max(chosen, key=lambda s: s.order) if chosen else None


def execute(graph: TaskGraph, record: bool = False, layout=None,
            disturbances: list[Disturbance] | None = None,
            order: list[int] | None = None,
            rules: list[monitor.OrderingRule] | None = None) -> ExecutionResult:
    scene = build_scene(graph.objects, layout=layout)
    robot = Panda(scene, record=record)
    result = ExecutionResult()
    pending = list(disturbances or [])

    # The confirmed procedure comes from the demonstration graph, regardless of the
    # order the robot actually runs the steps (so a reordered run can still be judged).
    rules = rules if rules is not None else monitor.derive_rules(graph)
    watched = [monitor.rel_key(r) for r in graph.final_relations]
    trace: monitor.Trace = []

    def snapshot(exec_index: int, action: str | None) -> None:
        trace.append(monitor.Snapshot(
            step_order=exec_index, action=action,
            holds={k: relations.holds(k[0], k[1], k[2], scene) for k in watched},
        ))

    snapshot(0, None)                                    # the initial scene
    by_order = {s.order: s for s in graph.steps}
    sequence = order if order is not None else sorted(by_order)

    for exec_index, step_order in enumerate(sequence, start=1):
        step = by_order[step_order]
        sr = _run_step(robot, scene, step)
        result.steps.append(StepOutcome(
            order=step.order, action=step.action, object=step.object,
            target=step.target, ok=sr.ok, residual=sr.residual, reason=sr.reason,
        ))
        snapshot(exec_index, f"{step.action} {step.object}")
        if not sr.ok:
            result.reason = f"step {step.order} ({step.action} {step.object}): {sr.reason}"
            break
        for dist in [d for d in pending if d.after_step == step.order]:
            result.disturbances.append(_apply_disturbance(robot, scene, dist))
            pending.remove(dist)
    else:
        result.completed = True

    result.procedure = monitor.check_rules(trace, rules)

    # Monitor and repair: restore any final relation that does not hold.
    if result.completed:
        for rnd in range(1, MAX_REPAIR_ROUNDS + 1):
            failing = [r for r in graph.final_relations
                       if not relations.holds(r.relation, r.a, r.b, scene)]
            if not failing:
                break
            fixed_any = False
            for r in failing:
                step = _establishing_step(graph, r)
                if step is None:
                    continue
                _run_step(robot, scene, step)
                restored = relations.holds(r.relation, r.a, r.b, scene)
                result.repairs.append(Repair(
                    relation=r.relation, a=r.a, b=r.b,
                    step_order=step.order, round=rnd, ok=restored,
                ))
                fixed_any = fixed_any or restored
            if not fixed_any:
                break        # nothing this round could be restored; stop retrying

    for r in graph.final_relations:
        result.final.append(RelationOutcome(
            relation=r.relation, a=r.a, b=r.b,
            holds=relations.holds(r.relation, r.a, r.b, scene),
        ))

    result.success = result.completed and all(r.holds for r in result.final)
    result.frames = robot.frames
    return result


def execute_file(path: Path, record: bool = False,
                 disturbances: list[Disturbance] | None = None,
                 reorder: bool = False,
                 rules: list[monitor.OrderingRule] | None = None) -> ExecutionResult:
    graph = TaskGraph.model_validate_json(Path(path).read_text(encoding="utf-8"))
    order = None
    if reorder:
        order = sorted((s.order for s in graph.steps), reverse=True)
    return execute(graph, record=record, disturbances=disturbances, order=order, rules=rules)
