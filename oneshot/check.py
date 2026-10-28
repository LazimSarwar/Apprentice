"""Check an attempt video against a reference video of the same procedure.

The reference is read into ordered *milestones* — the result of each step ("the red block
is in the left tray"). The attempt is read the same way, guided to reuse the reference's
object names, and every milestone is located in time. That gives four kinds of issue, each
with the moment in the attempt video to look at:

  skipped      — a milestone never happens
  wrong_place  — the right object went somewhere else
  order        — two milestones happen in the wrong order (via the temporal monitor)
  extra        — something the reference never did (a warning, not a failure)

Run:  python -m oneshot.check REFERENCE ATTEMPT        (videos or graph JSON files)
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .schema import TaskGraph
from .sim import monitor
from .sim.executor import sanitize_plan
from .sim.scene import _is_container
from .words import clock, goal_text, nice, step_text, where

Key = tuple[str, str, str]                     # (relation, object, target)


@dataclass
class Milestone:
    key: Key
    text: str              # the reference step, in plain English
    ref_time: float        # when it is reached in the reference video


@dataclass
class Issue:
    kind: str              # skipped | wrong_place | order | extra
    severity: str          # fail | warn
    message: str
    time: float | None     # seconds into the attempt video
    objects: list[str] = field(default_factory=list)


@dataclass
class Report:
    passed: bool
    milestones: list[dict]
    issues: list[Issue]
    mapping: dict[str, str]

    def summary(self) -> str:
        fails = [i for i in self.issues if i.severity == "fail"]
        head = "PASS - the attempt follows the procedure" if self.passed else \
            f"FAIL - {len(fails)} problem{'s' if len(fails) != 1 else ''}"
        lines = [head]
        for m in self.milestones:
            mark = "ok " if m["done"] else "-- "
            lines.append(f"  [{mark}] {m['text']}" + (f"  @ {clock(m['time'])}" if m["done"] else ""))
        for i in self.issues:
            lines.append(f"  {'!!' if i.severity == 'fail' else ' ~'} {clock(i.time) if i.time is not None else '   '}  {i.message}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "milestones": self.milestones,
                "issues": [asdict(i) for i in self.issues], "mapping": self.mapping}


_DEFAULT_REL = {"stack": "on"}


def _relation(step, graph: TaskGraph) -> str | None:
    if step.relation:
        return step.relation
    if step.action in _DEFAULT_REL:
        return _DEFAULT_REL[step.action]
    target = next((o for o in graph.objects if o.id == step.target), None)
    return "inside" if target is not None and _is_container(target) else "near"


def milestones(ref: TaskGraph) -> list[Milestone]:
    """Ordered, de-duplicated results of the reference's (sanitised) steps."""
    steps, _, _ = sanitize_plan(ref)
    out: list[Milestone] = []
    seen: set[Key] = set()
    for s in sorted(steps, key=lambda s: s.order):
        if not s.target:
            continue
        key = (_relation(s, ref), s.object, s.target)
        if key not in seen:
            seen.add(key)
            out.append(Milestone(key, step_text(s), s.end_s))
    return out


# --- matching the attempt's objects to the reference's -----------------------

def _color(o) -> str:
    c = (o.color or "").lower()
    return {"grey": "gray"}.get(c, c)


def _mods(obj_id: str) -> set[str]:
    return {p for p in obj_id.split("_") if p in ("left", "right", "top", "middle", "bottom", "front", "back", "center")}


def map_objects(ref: TaskGraph, att: TaskGraph) -> dict[str, str]:
    """attempt id -> reference id. Exact id first (guided extraction makes this the norm),
    then same colour and kind, using left/right/top-style modifiers to break ties."""
    ref_ids = {o.id for o in ref.objects}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for a in att.objects:
        if a.id in ref_ids:
            mapping[a.id] = a.id
            used.add(a.id)
    for a in att.objects:
        if a.id in mapping:
            continue
        cands = [r for r in ref.objects if r.id not in used
                 and _color(r) == _color(a) and _is_container(r) == _is_container(a)]
        if len(cands) > 1:
            cands.sort(key=lambda r: -len(_mods(r.id) & _mods(a.id)))
        if cands:
            mapping[a.id] = cands[0].id
            used.add(cands[0].id)
    return mapping


# --- the comparison ---------------------------------------------------------------

def compare(ref: TaskGraph, att: TaskGraph,
            rules: list[monitor.OrderingRule] | None = None) -> Report:
    ms = milestones(ref)
    ms_keys = [m.key for m in ms]
    mapping = map_objects(ref, att)
    to_ref = lambda x: mapping.get(x, x)

    events: list[tuple[float, Key, object]] = []
    for s in sorted(att.steps, key=lambda s: (s.end_s, s.order)):
        if s.target:
            events.append((s.end_s, (_relation(s, att), to_ref(s.object), to_ref(s.target)), s))
    first: dict[Key, float] = {}
    for t, k, _ in events:
        first.setdefault(k, t)
    finals = {(r.relation, to_ref(r.a), to_ref(r.b)) for r in att.final_relations}

    issues: list[Issue] = []
    explained: set[Key] = set()             # attempt events accounted for by a wrong_place
    prev_time: float | None = None
    for m in ms:
        if m.key in first:
            prev_time = first[m.key]
            continue
        if m.key in finals:                  # reached, but no step said when
            continue
        rel, a, b = m.key
        moved_elsewhere = [(t, k) for t, k, _ in events if k[1] == a and k not in ms_keys and k not in explained]
        if moved_elsewhere:
            t, k = moved_elsewhere[0]
            explained.add(k)
            issues.append(Issue("wrong_place", "fail",
                                f"Put the {nice(a)} {where(k[0], k[2])} instead of {where(rel, b)}",
                                t, [a, k[2], b]))
        else:
            hint = f" (expected after {clock(prev_time)})" if prev_time is not None else ""
            issues.append(Issue("skipped", "fail", f"Skipped: {m.text}{hint}", prev_time, [a, b]))

    # Order: replay when each milestone was reached through the temporal monitor.
    rules = rules if rules is not None else [
        monitor.OrderingRule(before=ms_keys[i], after=ms_keys[i + 1]) for i in range(len(ms_keys) - 1)]
    times = sorted({first[k] for k in ms_keys if k in first})
    trace = [monitor.Snapshot(0, None, {k: False for k in ms_keys})]
    for i, t in enumerate(times, start=1):
        trace.append(monitor.Snapshot(i, None, {k: (k in first and first[k] <= t) for k in ms_keys}))
    text = {m.key: m.text for m in ms}
    for rule, verdict in zip(rules, monitor.check_rules(trace, rules)):
        if verdict.ok or rule.before not in first or rule.after not in first:
            continue                         # a skip is already reported; don't double count
        issues.append(Issue("order", "fail",
                            f"Wrong order: \"{text.get(rule.after, goal_text(*rule.after))}\" happened before "
                            f"\"{text.get(rule.before, goal_text(*rule.before))}\"",
                            first[rule.after], [rule.after[1], rule.before[1]]))

    for t, k, s in events:
        if k not in ms_keys and k not in explained:
            issues.append(Issue("extra", "warn", f"Extra step not in the reference: {step_text(s)}", t, [k[1]]))

    issues.sort(key=lambda i: (i.time is None, i.time or 0.0))
    status = [{"text": m.text, "done": m.key in first or m.key in finals, "time": first.get(m.key),
               "ref_time": m.ref_time} for m in ms]
    passed = not any(i.severity == "fail" for i in issues)
    return Report(passed=passed, milestones=status, issues=issues, mapping=mapping)


# --- CLI -----------------------------------------------------------------------------

def _load_or_build(path: Path, provider: str, track: bool, guide: TaskGraph | None) -> TaskGraph:
    if path.suffix.lower() == ".json":
        return TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))
    from .clients import ROOT
    from .graph import build_task_graph
    known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in guide.objects] if guide else None
    g = build_task_graph(path, provider=provider, known_objects=known, track=track)
    out = ROOT / "runs" / f"{path.stem}.graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(g.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"read {path.name} -> {out.name}")
    return g


def main() -> None:
    p = argparse.ArgumentParser(description="Check an attempt video against a reference video")
    p.add_argument("reference", type=Path, help="reference video or its graph JSON")
    p.add_argument("attempt", type=Path, nargs="+", help="attempt video(s) or graph JSON(s)")
    p.add_argument("--provider", choices=["hosted", "fireworks"], default="hosted")
    p.add_argument("--no-track", action="store_true", help="skip object tracking (faster, no evidence)")
    p.add_argument("--json", type=Path, help="also write the report(s) as JSON")
    args = p.parse_args()

    ref = _load_or_build(args.reference, args.provider, not args.no_track, None)
    reports = {}
    for path in args.attempt:
        att = _load_or_build(path, args.provider, not args.no_track, ref)
        report = compare(ref, att)
        reports[path.name] = report.to_dict()
        print(f"\n=== {path.name} ===\n{report.summary()}")
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
