"""Check an attempt video against the procedure learned from a reference video.

The reference is read into ordered *milestones* — the result of each step ("the liner is in
the box") — and into a procedure (`procedure.learn`): which orders are required, which are
free, each step's spoken reason and whether it is critical. The attempt is read the same way,
guided to reuse the reference's object names, and every milestone is located in time:

  skipped      — a milestone never happens
  wrong_place  — the right object went somewhere else
  order        — a required order broken (a failure) or a merely different order (a note)
  extra        — something the reference never did (a note)
  unknown      — a milestone looks missing, but its object was hidden for most of the video,
                 so the checker says it couldn't verify it instead of guessing
  doubt        — the tracker's boxes disagree with what the video model reported

When the attempt carries tracking evidence, every reached milestone is also checked against
the tracker's boxes (`confirmed` / `seen` / `unseen` / `contradicted`).

Run:  python -m oneshot.check REFERENCE ATTEMPT...  [--also SECOND_REFERENCE]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .narration import Narration, Turn, listen, narrate, spoken_text
from .procedure import (Key, Milestone, Procedure, Rule, events, final_keys, learn,  # noqa: F401
                        map_objects, milestones, pair_id)
from .schema import TaskGraph
from .sim import monitor
from .words import clock, imperative, into, nice, where

HIDDEN = 0.25          # seen in under this share of the frames after the previous step = hidden
SETTLE = 0.5           # an object must be seen this long after its move ends to know where it came to rest


@dataclass
class Issue:
    kind: str              # skipped | wrong_place | order | extra | unknown | doubt
    severity: str          # fail | warn | unknown
    message: str
    time: float | None     # seconds into the attempt video
    objects: list[str] = field(default_factory=list)
    critical: bool = False
    reason: str | None = None      # why it matters (spoken in the reference, or the rule's basis)
    quote: str | None = None       # the exact words, when it was said
    fix: str | None = None


@dataclass
class Report:
    passed: bool
    verdict: str                   # pass | pass_notes | unsure | fail
    milestones: list[dict]
    issues: list[Issue]
    mapping: dict[str, str]
    rules: list[dict] = field(default_factory=list)
    verified: bool = False         # all reached milestones corroborated by tracking
    tracking_used: bool = False
    evidence_status: str = "model_only"  # model_only | partial | corroborated | inconclusive

    def summary(self) -> str:
        fails = [i for i in self.issues if i.severity == "fail"]
        head = {"pass": "PASS - the attempt follows the procedure",
                "pass_notes": "PASS (with notes)",
                "unsure": "CAN'T VERIFY - review the evidence before accepting",
                "fail": f"FAIL - {len(fails)} problem{'s' if len(fails) != 1 else ''}"}[self.verdict]
        lines = [head]
        for m in self.milestones:
            mark = "ok " if m["done"] else "-- "
            ev = f"  [{m['evidence']}]" if m.get("evidence") else ""
            lines.append(f"  [{mark}] {m['text']}" + (f"  @ {clock(m['time'])}" if m["done"] else "") + ev)
        for i in self.issues:
            flag = {"fail": "!!", "warn": " ~", "unknown": " ?"}[i.severity]
            lines.append(f"  {flag} {clock(i.time) if i.time is not None else '   '}  "
                         f"{'CRITICAL ' if i.critical else ''}{i.message}")
            if i.quote:
                lines.append(f"         said: \"{i.quote}\"")
            if i.fix:
                lines.append(f"         fix: {i.fix}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "verdict": self.verdict, "milestones": self.milestones,
                "issues": [asdict(i) for i in self.issues], "mapping": self.mapping,
                "rules": self.rules, "verified": self.verified, "tracking_used": self.tracking_used,
                "evidence_status": self.evidence_status}


# --- tracking evidence -------------------------------------------------------------

def _frames(g: TaskGraph) -> int:
    return g.video.frames or max(1, int(g.video.duration_s * (g.video.fps or 30)))


def _visible_share(obj, g: TaskGraph, since: float | None) -> float:
    """Share of the frames the tracker sampled after `since` in which `obj` was seen —
    i.e. could the tracker have seen this step happen at all?"""
    if obj is None or not obj.track:
        return 0.0
    t0 = since or 0.0
    sampled = {b.frame for o in g.objects if o.track for b in o.track.boxes if b.t_s >= t0}
    seen = {b.frame for b in obj.track.boxes if b.t_s >= t0}
    return len(seen) / len(sampled) if sampled else 0.0


def _box_near(track, t: float, before: float = 0.3, after: float = 1.5):
    boxes = track.in_window(t - before, t + after) if track else []
    return boxes[-1] if boxes else None


def _area(a: list[float]) -> float:
    return max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])


def _inside_share(a: list[float], b: list[float]) -> float:
    """Share of box a that lies inside box b."""
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return w * h / area


def _gap(a: list[float], b: list[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return (dx * dx + dy * dy) ** 0.5


def _evidence(key: Key, step, att: TaskGraph, back: dict[str, str]) -> str:
    """What the tracker says about a reached milestone."""
    rel, a, b = key
    objs = {o.id: o for o in att.objects}
    oa, ob = objs.get(back.get(a, a)), objs.get(back.get(b, b))
    if oa is None or not oa.track:
        return "unseen"
    box_a = _box_near(oa.track, step.end_s)
    if box_a is None:
        return "unseen"
    rest = step.end_s + SETTLE
    if box_a.t_s < rest and any(b.t_s >= rest for o in att.objects if o.track for b in o.track.boxes):
        return "unseen"                      # lost as the move ended (covered?): its resting place wasn't seen
    if ob is None or not ob.track:
        return "seen"
    t = box_a.t_s
    box_b = _box_near(ob.track, t, before=1.0, after=1.0)
    if box_b is None and rel == "inside":    # a container stays put: use where it was last seen
        box_b = next((bx for bx in reversed(ob.track.boxes) if bx.t_s <= t), None)
    if box_b is None:
        return "seen"
    if rel == "inside" and _area(box_a.box) > _area(box_b.box):
        return "unseen"                      # bigger than its container: the track latched onto something else
    if rel in ("on", "inside"):
        share = _inside_share(box_a.box, box_b.box)
        if share >= 0.5:
            return "confirmed"
        width = box_a.box[2] - box_a.box[0]
        return "contradicted" if share == 0 and _gap(box_a.box, box_b.box) > width else "seen"
    width = max(box_a.box[2] - box_a.box[0], box_b.box[2] - box_b.box[0])
    return "confirmed" if _gap(box_a.box, box_b.box) <= width else "seen"


# --- the comparison ---------------------------------------------------------------

def _q(text: str) -> str:
    return f"“{text}”"


def rule_view(r: Rule, proc: Procedure) -> dict:
    sa, sb = proc.step(r.before), proc.step(r.after)
    return {"id": pair_id(r.before, r.after), "strength": r.strength, "source": r.source, "why": r.why,
            "quote": r.quote, "before": sa.text if sa else str(r.before), "after": sb.text if sb else str(r.after)}


def compare(ref: TaskGraph, att: TaskGraph, rules: list[monitor.OrderingRule] | None = None,
            procedure: Procedure | None = None) -> Report:
    """Check `att` against the procedure (learned from `ref` alone when not given).
    Explicit `rules` replace the learned ordering with required rules (e.g. a confirmed spec)."""
    proc = procedure or learn([ref])
    if rules is not None:
        proc = Procedure(proc.steps, [Rule(r.before, r.after, "required", "you", r.note) for r in rules],
                         proc.references, proc.narration)
    ms = proc.steps
    if not ms:
        return Report(False, "unsure", [], [Issue(
            "invalid_reference", "unknown", "No checkable steps were learned from the reference.", None,
            fix="Record a clear reference showing each placement, then confirm the learned steps.")], {},
            evidence_status="inconclusive")
    ms_keys = [m.key for m in ms]
    info = {m.key: m for m in ms}
    mapping = map_objects(ref, att)
    back = {v: k for k, v in mapping.items()}           # reference id -> attempt id
    evs = events(att, mapping)
    first: dict[Key, float] = {}
    step_of: dict[Key, object] = {}
    for t, k, s in evs:
        if k not in first:
            first[k], step_of[k] = t, s
    finals = final_keys(att, mapping)
    tracked = att.meta.track_seconds is not None or any(o.track for o in att.objects)
    objs = {o.id: o for o in att.objects}

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
        moved_elsewhere = [(t, k) for t, k, _ in evs if k[1] == a and k not in ms_keys and k not in explained]
        obj = objs.get(back.get(a, a))
        if moved_elsewhere:
            t, k = moved_elsewhere[0]
            explained.add(k)
            step = next(s for tt, kk, s in evs if kk == k)
            if tracked and _evidence(k, step, att, back) in ("unseen", "contradicted"):
                issues.append(Issue("unknown", "unknown",
                                    f"Couldn't verify where the {nice(a)} went — the tracker could not support "
                                    f"the reported placement ({where(k[0], k[2])})", t, [a, k[2], b],
                                    m.critical, m.reason, m.quote,
                                    "Re-record with the whole workspace in view, or check this step by hand."))
                continue
            issues.append(Issue("wrong_place", "fail",
                                f"Put the {nice(a)} {where(k[0], k[2])} instead of {where(rel, b)}",
                                t, [a, k[2], b], m.critical, m.reason, m.quote,
                                f"Move the {nice(a)} {into(rel, b)}."))
        elif tracked and _visible_share(obj, att, prev_time) < HIDDEN:
            issues.append(Issue("unknown", "unknown",
                                f"Couldn't verify: {m.text} — the {nice(a)} was hidden or out of view "
                                f"when this step should have happened", prev_time, [a, b], m.critical, m.reason, m.quote,
                                "Re-record with the whole workspace in view, or check this step by hand."))
        else:
            hint = f" (expected after {clock(prev_time)})" if prev_time is not None else ""
            issues.append(Issue("skipped", "fail", f"Skipped: {m.text}{hint}", prev_time, [a, b],
                                m.critical, m.reason, m.quote, f"{imperative(m.text)}."))

    # Order: replay when each milestone was reached through the temporal monitor, against
    # every pair whose order is required or was only shown (free pairs are not checked).
    enforced = [r for r in proc.enforced() if r.before in info and r.after in info]
    mon = [monitor.OrderingRule(r.before, r.after, r.strength) for r in enforced]
    times = sorted({first[k] for k in ms_keys if k in first})
    trace = [monitor.Snapshot(0, None, {k: False for k in ms_keys})]
    for i, t in enumerate(times, start=1):
        trace.append(monitor.Snapshot(i, None, {k: (k in first and first[k] <= t) for k in ms_keys}))
    broken = [r for r, v in zip(enforced, monitor.check_rules(trace, mon))
              if not v.ok and r.before in first and r.after in first]   # skips are reported already
    issues += _order_issues(broken, first, info)

    for t, k, s in evs:
        if k not in ms_keys and k not in explained:
            issues.append(Issue("extra", "warn", f"Extra step not in the reference: {k[1].replace('_', ' ')} "
                                f"{where(k[0], k[2])}", t, [k[1]]))

    status = []
    for m in ms:
        done = m.key in first or m.key in finals
        ev = None
        if tracked and done:
            ev = _evidence(m.key, step_of[m.key], att, back) if m.key in step_of else "unseen"
            failed = [k for k in m.key[1:] if back.get(k, k) in att.meta.tracking_errors]
            if failed:
                ev = "unseen"
                issues.append(Issue("unknown", "unknown",
                                    f"Couldn't verify: {m.text} — tracking was unavailable for "
                                    + ", ".join(nice(k) for k in failed) + ".", first.get(m.key),
                                    list(m.key[1:]), m.critical,
                                    fix="Review this step by hand or retry the check."))
            elif ev == "contradicted":
                issues.append(Issue("doubt", "unknown",
                                    f"Check this step: the tracker shows the {nice(m.key[1])} ending up away "
                                    f"from the {nice(m.key[2])}", first.get(m.key), [m.key[1], m.key[2]], m.critical,
                                    fix="Review this moment or re-record with both objects in view."))
            elif ev == "unseen":
                issues.append(Issue("unknown", "unknown",
                                    f"Couldn't verify: {m.text} — the reported result wasn't seen by the tracker.",
                                    first.get(m.key), [m.key[1], m.key[2]], m.critical,
                                    fix="Check this step by hand or re-record with the objects visible after placement."))
        status.append({"text": m.text, "done": done, "time": first.get(m.key), "ref_time": m.ref_time,
                       "critical": m.critical, "reason": m.reason, "quote": m.quote, "evidence": ev})

    # A final picture cannot establish the order of an untimed placement.
    untimed = {k for k in ms_keys if k in finals and k not in first}
    for k in untimed:
        if any(r.strength == "required" and k in (r.before, r.after) for r in enforced):
            issues.append(Issue("unknown", "unknown", f"Couldn't verify when this step happened: {info[k].text}.",
                                None, [k[1]], info[k].critical,
                                fix="Show the full sequence so the required order can be checked."))

    issues.sort(key=lambda i: (i.time is None, i.time or 0.0))
    sev = {i.severity for i in issues}
    verdict = "fail" if "fail" in sev else "unsure" if "unknown" in sev else "pass_notes" if sev else "pass"
    corroborated = bool(status) and all(m["done"] and m["evidence"] == "confirmed" for m in status)
    evidence_status = ("inconclusive" if "unknown" in sev else "corroborated" if corroborated
                       else "partial" if tracked else "model_only")
    return Report(passed=verdict in ("pass", "pass_notes"), verdict=verdict, milestones=status, issues=issues,
                  mapping=mapping, rules=[rule_view(r, proc) for r in proc.shown()], verified=corroborated,
                  tracking_used=tracked, evidence_status=evidence_status)


def _order_issues(broken: list[Rule], first: dict[Key, float], info: dict) -> list[Issue]:
    """One issue per misplaced step: group the broken pairs on the step they share most."""
    out: list[Issue] = []
    for level in ("required", "as_shown"):
        left = [r for r in broken if r.strength == level]
        if level == "as_shown":                  # a step already failing on order needs no extra note
            failing = {k for i in out for k in i.objects}
            left = [r for r in left if r.before[1] not in failing and r.after[1] not in failing]
        while left:
            counts: dict[tuple[Key, str], int] = {}
            for r in left:
                counts[(r.after, "early")] = counts.get((r.after, "early"), 0) + 1
                counts[(r.before, "late")] = counts.get((r.before, "late"), 0) + 1
            (key, side), _ = max(counts.items(), key=lambda kv: (kv[1], kv[0][1] == "early"))
            group = [r for r in left if (r.after if side == "early" else r.before) == key]
            left = [r for r in left if r not in group]
            others = [r.before if side == "early" else r.after for r in group]
            names = " and ".join(_q(info[k].text) for k in others)
            step = info[key]
            if side == "early":
                msg = f"{_q(step.text)} happened before {names}"
                time = first[key]
                fix = " and ".join(imperative(info[k].text) for k in others) + " first."
            else:
                msg = f"{_q(step.text)} happened after {names}"
                time = min(first[k] for k in others)
                fix = f"{imperative(step.text)} earlier, before {names}."
            said = next((r.quote for r in group if r.quote), None)
            crit = step.critical or any(info[k].critical for k in others)
            spoken = next((s.reason for s in (step, *[info[k] for k in others]) if s.critical and s.reason), None)
            if level == "required":
                out.append(Issue("order", "fail", "Wrong order: " + msg, time, [key[1], *[k[1] for k in others]],
                                 crit, spoken or (None if said else group[0].why), said, fix))
            else:
                out.append(Issue("order", "warn", "Different order from the reference: " + msg, time,
                                 [key[1], *[k[1] for k in others]], False,
                                 "Nothing says this order matters. Mark it as required if it does.", None, None))
    return out


# --- CLI -----------------------------------------------------------------------------

def _stem(path: Path) -> str:
    if path.suffix.lower() != ".json":
        return path.stem
    return path.name.removesuffix(".json").removesuffix(".graph").removesuffix(".tracked")


def _load_or_build(path: Path, provider: str, track: bool, guide: TaskGraph | None,
                   spoken: str | None = None) -> TaskGraph:
    if path.suffix.lower() == ".json":
        return TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))
    from .clients import ROOT
    from .graph import build_task_graph
    known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in guide.objects] if guide else None
    g = build_task_graph(path, provider=provider, known_objects=known, track=track, spoken=spoken)
    out = ROOT / "runs" / f"{path.stem}.graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(g.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"read {path.name} -> {out.name}")
    return g


def load_turns(reference: Path) -> list[Turn] | None:
    """What was said in the reference, cached as runs/<stem>.turns.json."""
    from .clients import ROOT
    cache = ROOT / "runs" / f"{_stem(reference)}.turns.json"
    if cache.exists():
        return [Turn(**t) for t in json.loads(cache.read_text(encoding="utf-8"))] or None
    if reference.suffix.lower() == ".json":
        return None
    turns = listen(reference, ROOT / "runs" / "audio")
    cache.write_text(json.dumps([asdict(t) for t in turns or []], indent=2), encoding="utf-8")
    if turns:
        print(f"heard {len(turns)} spoken line(s) in {reference.name}")
    return turns


def load_narration(reference: Path, ref: TaskGraph, turns: list[Turn] | None) -> Narration | None:
    """The narration read against the reference's steps, cached as runs/<stem>.narration.json."""
    from .clients import ROOT
    cache = ROOT / "runs" / f"{_stem(reference)}.narration.json"
    if cache.exists():
        return Narration.from_dict(json.loads(cache.read_text(encoding="utf-8")))
    n = narrate(turns, [(m.text, m.start, m.ref_time) for m in milestones(ref)])
    if n is not None:
        cache.write_text(json.dumps(n.to_dict(), indent=2), encoding="utf-8")
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="Check an attempt video against a reference video")
    p.add_argument("reference", type=Path, help="reference video or its graph JSON")
    p.add_argument("attempt", type=Path, nargs="+", help="attempt video(s) or graph JSON(s)")
    p.add_argument("--also", type=Path, action="append", default=[],
                   help="another correct reference (video or graph JSON); teaches which orders are free")
    p.add_argument("--provider", choices=["hosted", "fireworks", "baseten"], default="hosted")
    p.add_argument("--no-track", action="store_true", help="skip object tracking (faster, no evidence)")
    p.add_argument("--no-narration", action="store_true", help="ignore what was said in the reference")
    p.add_argument("--procedure-out", type=Path, help="write the learned procedure as JSON")
    p.add_argument("--json", type=Path, help="also write the report(s) as JSON")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    turns = None if args.no_narration else load_turns(args.reference)
    ref = _load_or_build(args.reference, args.provider, not args.no_track, None, spoken_text(turns))
    extra = [_load_or_build(a, args.provider, False, ref) for a in args.also]
    narration = None if args.no_narration else load_narration(args.reference, ref, turns)
    proc = learn([ref, *extra], narration)
    print("\nprocedure:")
    for s in proc.steps:
        print(f"  {'!' if s.critical else '-'} {s.text}" + (f'   "{s.quote}"' if s.quote else ""))
    for r in proc.shown():
        print(f"  [{r.strength:8}] {proc.step(r.before).text}  ->  {proc.step(r.after).text}   ({r.source})")
    if args.procedure_out:
        args.procedure_out.write_text(json.dumps(proc.to_dict(), indent=2), encoding="utf-8")
    reports = {}
    for path in args.attempt:
        att = _load_or_build(path, args.provider, not args.no_track, ref)
        report = compare(ref, att, procedure=proc)
        reports[path.name] = report.to_dict()
        print(f"\n=== {path.name} ===\n{report.summary()}")
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
