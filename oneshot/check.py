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
from types import SimpleNamespace

from .narration import Narration, Turn, listen, narrate, spoken_text
from .procedure import (Key, Milestone, Procedure, Rule, events, final_keys, learn,  # noqa: F401
                        map_objects, milestones, pair_id, placement_timeline, with_confirmed_kinds, identity_conflicts)
from .schema import TaskGraph
from .tracking_identity import ambiguous_tracks
from .sim import monitor
from .words import clock, imperative, into, nice, where

HIDDEN = 0.25          # seen in under this share of the frames after the previous step = hidden
SETTLE = 0.5           # an object must be seen this long after its move ends to know where it came to rest
ARRIVAL_TOLERANCE = 0.6  # near its resting place = within this share of the object's size
ARRIVAL_STAY = 0.85      # arrived = at least this share of the later boxes stay near the resting place
ARRIVAL_MARGIN = 1.0     # seconds between two arrivals before the tracker can call their order
ALREADY_THERE = 0.5      # settled before this many seconds: the track shows no arrival at all
STILL_SPAN = 0.3         # the box never moved more than this share of the object's size
COVER_OVERLAP = 0.25     # a covering object must overlap this share of where the item was resting
FULL_VIEW = 0.4          # a box under this share of the usual area shows a partly hidden object


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
    check_key: list[str] | None = None     # the expected placement a closer look can test
    reported_key: list[str] | None = None  # where the reading says the object went instead


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
    final_states: list[dict] = field(default_factory=list)
    order_checks: list[dict] = field(default_factory=list)
    second_looks: list[dict] = field(default_factory=list)

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
                "evidence_status": self.evidence_status, "final_states": self.final_states,
                "order_checks": self.order_checks, "second_looks": self.second_looks}


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
    if any(back.get(oid, oid) in att.meta.tracking_errors for oid in (a, b)):
        return "unseen"
    objs = {o.id: o for o in att.objects}
    oa, ob = objs.get(back.get(a, a)), objs.get(back.get(b, b))
    if oa is None or not oa.track:
        return "unseen"
    box_a = _box_near(oa.track, step.end_s)
    if box_a is None:
        return "unseen"
    # A query can switch identities only after an occlusion. Check the current
    # evidence window as well as the whole trajectory before trusting its box.
    local_duplicates = ambiguous_tracks(att.objects, box_a.t_s - 1.0, box_a.t_s)
    if any(oid in local_duplicates for oid in (oa.id, ob.id if ob else b)):
        return "unseen"
    rest = step.end_s + SETTLE
    if box_a.t_s < rest and any(b.t_s >= rest for o in att.objects if o.track for b in o.track.boxes):
        return "unseen"                      # lost as the move ended (covered?): its resting place wasn't seen
    if ob is None or not ob.track:
        return "seen"
    if oa.id != ob.id and (oa.track is ob.track or oa.track.boxes == ob.track.boxes):
        return "unseen"  # One trajectory is not independent evidence for two objects.
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


def _median(values: list[float]) -> float:
    return sorted(values)[len(values) // 2]


def _centre(box: list[float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _object_size(boxes) -> float:
    return _median([max(b.box[2] - b.box[0], b.box[3] - b.box[1]) for b in boxes])


def _full_view(boxes) -> list:
    """Boxes that show the whole object. A partly covered object shrinks to a sliver whose
    centre has moved, which must not read as the object itself moving."""
    if not boxes:
        return []
    areas = sorted(_area(b.box) for b in boxes)
    usual = areas[int(0.8 * (len(areas) - 1))]   # Not the median: a covered object can be a sliver for most of a clip.
    return [b for b in boxes if _area(b.box) >= FULL_VIEW * usual]


def arrival_time(track, duration: float) -> float | None:
    """When the tracker shows an object reaching the place it ends up, independent of the
    video model. None when the track is too short, is lost well before the end, or shows the
    object already there from the start, which says nothing about when it arrived."""
    boxes = track.boxes if track else []
    if len(boxes) < 5 or duration - boxes[-1].t_s > max(1.5, 0.15 * duration):
        return None
    boxes = _full_view(boxes)
    if len(boxes) < 5:
        return None
    tail = boxes[-max(5, len(boxes) // 8):]
    rest = (_median([_centre(b.box)[0] for b in tail]), _median([_centre(b.box)[1] for b in tail]))
    reach = ARRIVAL_TOLERANCE * _object_size(tail)
    near = [((_centre(b.box)[0] - rest[0]) ** 2 + (_centre(b.box)[1] - rest[1]) ** 2) ** 0.5 <= reach for b in boxes]
    for i, box in enumerate(boxes):
        if near[i] and sum(near[i:]) / (len(near) - i) >= ARRIVAL_STAY:
            return box.t_s if box.t_s >= ALREADY_THERE else None
    return None


def never_moved(track, step) -> bool:
    """The object was visible before and after its reported move and stayed in one place."""
    boxes = track.boxes if track else []
    if sum(b.t_s < step.start_s for b in boxes) < 3 or sum(b.t_s >= step.end_s for b in boxes) < 3:
        return False
    xs, ys = zip(*(_centre(b.box) for b in boxes))
    return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5 < STILL_SPAN * _object_size(boxes)


def _resting_box(track, start: float, end: float) -> list[float] | None:
    # Median of each edge, so one odd frame cannot decide the result.
    boxes = track.in_window(start, end)
    return [_median([b.box[i] for b in boxes]) for i in range(4)] if boxes else None


def _is_covered(key: Key, final_evidence: dict, required, step_of: dict, milestone_evidence: dict,
                objs: dict, back: dict[str, str], errors, visited: tuple = ()) -> bool:
    """An item can be out of sight because the procedure puts something on it. That counts
    only if its placement was confirmed first, it stayed visible until the covering step
    began, and the covering object is over it at the moment it disappears."""
    item = objs.get(back.get(key[1], key[1]))
    placed = step_of.get(key)
    if (item is None or not item.track or not item.track.boxes or placed is None
            or item.id in errors or milestone_evidence.get(key) != "confirmed"):
        return False
    for cover in required:
        if cover == key or cover in visited or cover[0] not in ("on", "inside") or cover[2] != key[1] or cover not in step_of:
            continue
        evidence = final_evidence.get(cover)
        if evidence == "unseen":
            evidence_ok = _is_covered(cover, final_evidence, required, step_of, milestone_evidence,
                                      objs, back, errors, (*visited, key))
        else:
            evidence_ok = evidence in ("confirmed", "seen")
        cover_obj = objs.get(back.get(cover[1], cover[1]))
        if not evidence_ok or cover_obj is None or not cover_obj.track or not cover_obj.track.boxes:
            continue
        whole = _full_view(item.track.boxes)
        if not whole or whole[-1].t_s < step_of[cover].start_s - 1.0:
            continue                         # It vanished well before anything covered it.
        # Compare at the moment it disappeared: the camera may shift later in the recording.
        gone = whole[-1].t_s
        last_seen = [_median([b.box[i] for b in whole if b.t_s >= gone - 1.0]) for i in range(4)]
        over = _resting_box(cover_obj.track, gone, gone + 1.5)
        if over and max(_inside_share(last_seen, over), _inside_share(over, last_seen)) >= COVER_OVERLAP:
            return True
    return False


# --- the comparison ---------------------------------------------------------------

def _q(text: str) -> str:
    return f"“{text}”"


def rule_view(r: Rule, proc: Procedure) -> dict:
    sa, sb = proc.step(r.before), proc.step(r.after)
    return {"id": pair_id(r.before, r.after), "strength": r.strength, "source": r.source, "why": r.why,
            "quote": r.quote, "before": sa.text if sa else str(r.before), "after": sb.text if sb else str(r.after)}


def _inside_container(placement: Key, container: str, locations: dict[str, Key]) -> bool:
    """Follow observed supports to an enclosing container, with cycle protection.

    Locations must describe the attempt at the relevant time. A placement on the
    container itself does not establish containment.
    """
    relation, _, target = placement
    visited = set()
    while target not in visited:
        if relation not in ("on", "inside"):
            return False
        if target == container:
            return relation == "inside"
        if target not in locations:
            return False
        visited.add(target)
        relation, _, target = locations[target]
    return False


def holds_now(key: Key, locations: dict[str, Key | None]) -> bool:
    """Does this placement hold given where every object currently is? A coarser description
    of the same placement ("in the box" for something on a liner in that box) still counts:
    a reading that is merely less specific is not evidence that the object moved."""
    current = locations.get(key[1])
    if current is None:
        return False
    return (current == key
            or (key[0] == "inside" and _inside_container(current, key[2], locations))
            or (current[0] == "inside" and _inside_container(key, current[2], locations)))


def _reached_by(key: Key, keys: set[Key], locations: dict[str, Key | None]) -> set[Key]:
    return {key} | {k for k in keys if k[0] == "inside" and k[1] == key[1] and k != key
                    and _inside_container(key, k[2], locations)}


def _kept_in_reference(ref: TaskGraph, rule: Rule, keys: set[Key]) -> bool:
    """Was the earlier step still in place when the reference reached the later one?"""
    locations: dict[str, Key | None] = {}
    for _, obj, key in placement_timeline(ref, {}):
        locations[obj] = key
        if key is not None and rule.after in _reached_by(key, keys, locations):
            return rule.before[1] not in locations or holds_now(rule.before, locations)
    return True


def order_checks(ref: TaskGraph, att: TaskGraph, proc: Procedure, first: dict[Key, float],
                 back: dict[str, str]) -> list[dict]:
    """Compare each required order the video model reports with the order the tracker shows."""
    keys = {s.key for s in proc.steps}
    objs = {o.id: o for o in att.objects}
    out = []
    for rule in proc.pairs:
        if (rule.strength != "required" or rule.before[1] == rule.after[1] or rule.before not in first
                or rule.after not in first or not _kept_in_reference(ref, rule, keys)):
            continue
        a, b = (objs.get(back.get(k[1], k[1])) for k in (rule.before, rule.after))
        errors = att.meta.tracking_errors
        before_at = arrival_time(a.track, att.video.duration_s) if a is not None and a.id not in errors else None
        after_at = arrival_time(b.track, att.video.duration_s) if b is not None and b.id not in errors else None
        reading_ok = first[rule.before] <= first[rule.after]
        if before_at is None or after_at is None:
            tracker = "unknown"
        elif abs(after_at - before_at) < ARRIVAL_MARGIN:
            tracker = "too_close"
        else:
            tracker = "agrees" if (before_at < after_at) == reading_ok else "conflicts"
        out.append({"id": pair_id(rule.before, rule.after), "before": proc.step(rule.before).text,
                    "after": proc.step(rule.after).text, "reading_ok": reading_ok, "tracker": tracker,
                    "before_at": before_at, "after_at": after_at, "key_before": list(rule.before),
                    "key_after": list(rule.after)})
    return out


def undone_issues(ref: TaskGraph, att: TaskGraph, proc: Procedure, mapping: dict[str, str] | None = None) -> list[Issue]:
    """A required earlier step must still hold when the later step happens. Reaching it once
    is not enough: taking the liner out, adding the charger and putting the liner back ends in
    the same picture, but the charger went into a bare box. Only applies where the reference
    itself still had the earlier step in place at that moment."""
    keys = {s.key for s in proc.steps}
    rules = [r for r in proc.pairs if r.strength == "required" and r.before[1] != r.after[1]
             and r.before in keys and r.after in keys]
    if not rules:
        return []
    mapping = map_objects(ref, att) if mapping is None else mapping

    enforced = [r for r in rules if _kept_in_reference(ref, r, keys)]
    locations: dict[str, Key | None] = {}
    reached: set[Key] = set()
    undone: dict[Key, list[tuple[float, Rule]]] = {}
    for t, obj, key in placement_timeline(att, mapping):
        locations[obj] = key
        if key is None:
            continue
        now = _reached_by(key, keys, locations)
        for rule in enforced:
            if rule.after in now and rule.before in reached and not holds_now(rule.before, locations):
                undone.setdefault(rule.before, []).append((t, rule))
        reached |= now
    issues = []
    for before, found in undone.items():
        step = proc.step(before)
        later = [proc.step(k) for k in dict.fromkeys(r.after for _, r in found)]
        names = " and ".join(_q(s.text) for s in later)
        rule = next((r for _, r in found if r.quote), found[0][1])
        spoken = next((s.reason for s in (step, *later) if s.critical and s.reason), None)
        issues.append(Issue("order", "fail", f"Wrong order: {names} happened while {_q(step.text)} was no longer in place",
                            found[0][0], [before[1], *[s.key[1] for s in later]],
                            step.critical or any(s.critical for s in later),
                            spoken or (None if rule.quote else rule.why), rule.quote,
                            f"{imperative(step.text)} and leave it in place before {names}."))
    return issues


def _closer_look(att: TaskGraph, key: Key) -> str | None:
    """The closer-look result for this expected placement, when it does not back the problem."""
    for look in att.meta.second_looks:
        if tuple(look["key"]) == key and look["outcome"] in ("disputed", "unclear"):
            return look["outcome"]
    return None


def _closer_look_phrase(outcome: str, a: str, relation: str, target: str) -> str:
    if outcome == "disputed":
        return f"two closer looks show the {nice(a)} {where(relation, target)}"
    return "closer looks at this moment did not agree"


def compare(ref: TaskGraph, att: TaskGraph, rules: list[monitor.OrderingRule] | None = None,
            procedure: Procedure | None = None, *, final: bool = True) -> Report:
    """Check `att` against the procedure (learned from `ref` alone when not given).
    Explicit `rules` replace the learned ordering with required rules (e.g. a confirmed spec)."""
    proc = procedure or learn([ref])
    duplicates = ambiguous_tracks(att.objects)
    if duplicates:
        # Preserve raw tracks in the stored graph, but never use a shared identity
        # as evidence for presence, absence, wrong placement or final acceptance.
        att = att.model_copy(update={
            "objects": [o.model_copy(update={"track": None, "tracked": False})
                        if o.id in duplicates else o for o in att.objects],
            "meta": att.meta.model_copy(update={"tracking_errors": {
                **att.meta.tracking_errors,
                **{oid: "Ambiguous tracking identity" for oid in duplicates}},
                "track_seconds": att.meta.track_seconds if att.meta.track_seconds is not None else 0.0})})
    ref, att = with_confirmed_kinds(ref, proc), with_confirmed_kinds(att, proc)
    required_objects = {oid for step in proc.steps for oid in step.key[1:]}
    conflicts = {oid: reason for oid, reason in identity_conflicts(ref, att).items() if oid in required_objects}
    if conflicts:
        # Preserve the saved graph. A track of a conflicting identity cannot verify
        # a reference placement, even when steps/final relations reuse the same ID.
        att = att.model_copy(update={"meta": att.meta.model_copy(update={
            "tracking_errors": {**att.meta.tracking_errors,
                                **{oid: "Conflicting object identity" for oid in conflicts}}})})
    if rules is not None:
        proc = Procedure(proc.steps, [Rule(r.before, r.after, "required", "you", r.note) for r in rules],
                         proc.references, proc.narration, proc.final_goals, proc.object_kinds)
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
    locations: dict[str, Key] = {}
    entailed: set[Key] = set()
    for t, k, s in evs:
        locations[k[1]] = k
        if k not in first:
            first[k], step_of[k] = t, s
        for expected in ms_keys:
            if (expected[0] == "inside" and expected[1] == k[1] and expected != k
                    and _inside_container(k, expected[2], locations)):
                if expected not in first:
                    first[expected], step_of[expected] = t, s
                entailed.add(k)
    finals = final_keys(att, mapping)
    tracked = att.meta.track_seconds is not None or any(o.track for o in att.objects)
    objs = {o.id: o for o in att.objects}

    issues: list[Issue] = [Issue("identity", "unknown",
        f"Couldn't verify the identity of {nice(oid)} — {reason}.", None, [oid],
        fix="Compare this object with the reference and confirm it is the intended item.")
        for oid, reason in conflicts.items()]
    for obj in att.objects:
        if obj.id not in mapping and any((candidate.color or "").lower() == (obj.color or "").lower()
                                        for candidate in ref.objects):
            candidates = [candidate for candidate in ref.objects if candidate.id not in mapping.values()
                          and (candidate.color or "").lower() == (obj.color or "").lower()]
            if len(candidates) > 1:
                issues.append(Issue("unknown", "unknown", f"Object identity is ambiguous for {nice(obj.id)}.",
                                    None, [obj.id], fix="Use distinct objects or confirm their identities in the reference."))
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
            locations = {key[1]: key for time, key, _ in evs if time <= t}
            if k[0] == "inside" and _inside_container(m.key, k[2], locations):
                issues.append(Issue("unknown", "unknown",
                                    f"Couldn't verify: {m.text} — the video model reported the {nice(a)} "
                                    f"{where(k[0], k[2])}, which does not establish whether it is {where(rel, b)}.",
                                    t, [a, b, k[2]], m.critical, m.reason, m.quote,
                                    "Review the supporting object and this placement in the recording."))
                continue
            if tracked and _evidence(k, step, att, back) in ("unseen", "contradicted"):
                issues.append(Issue("unknown", "unknown",
                                    f"Couldn't verify where the {nice(a)} went — the tracker could not support "
                                    f"the reported placement ({where(k[0], k[2])})", t, [a, k[2], b],
                                    m.critical, m.reason, m.quote,
                                    "Re-record with the whole workspace in view, or check this step by hand."))
                continue
            closer = _closer_look(att, m.key)
            if closer:
                issues.append(Issue("unknown", "unknown",
                                    f"Couldn't verify: {m.text} — the first reading put the {nice(a)} {where(k[0], k[2])}, "
                                    f"but {_closer_look_phrase(closer, a, rel, b)}.", t, [a, k[2], b],
                                    m.critical, m.reason, m.quote, "Review this moment in the recording."))
                continue
            issues.append(Issue("wrong_place", "fail",
                                f"Put the {nice(a)} {where(k[0], k[2])} instead of {where(rel, b)}",
                                t, [a, k[2], b], m.critical, m.reason, m.quote,
                                f"Move the {nice(a)} {into(rel, b)}.", list(m.key), list(k)))
        elif tracked and obj is not None and obj.id in duplicates:
            issues.append(Issue("unknown", "unknown",
                                f"Couldn't verify: {m.text} — the tracker confused the {nice(a)} "
                                "with another object, so it cannot establish whether this step was skipped.",
                                prev_time, [a, b], m.critical, m.reason, m.quote,
                                "Review the object identities and this step by hand, or retry the check."))
        elif tracked and obj is not None and obj.from_reference and not obj.track and obj.id not in att.meta.tracking_errors:
            issues.append(Issue("unknown", "unknown",
                                f"Couldn't verify: {m.text} — the {nice(a)} was not found anywhere in this recording.",
                                prev_time, [a, b], m.critical, m.reason, m.quote,
                                "Check whether it was left out, or re-record with the whole workspace in view."))
        elif tracked and _visible_share(obj, att, prev_time) < HIDDEN:
            issues.append(Issue("unknown", "unknown",
                                f"Couldn't verify: {m.text} — the {nice(a)} was hidden or out of view "
                                f"when this step should have happened", prev_time, [a, b], m.critical, m.reason, m.quote,
                                "Re-record with the whole workspace in view, or check this step by hand."))
        elif tracked and _evidence(m.key, SimpleNamespace(end_s=max(0.0, att.video.duration_s - 1.5)), att, back) == "confirmed":
            # A reading can miss a step that happened. It takes both witnesses to call it skipped.
            issues.append(Issue("unknown", "unknown",
                                f"Couldn't verify: {m.text} — the video reading does not report it, but the tracker "
                                f"shows the {nice(a)} in that place at the end.", prev_time, [a, b], m.critical,
                                m.reason, m.quote, "Review the recording to confirm this step happened."))
        else:
            hint = f" (expected after {clock(prev_time)})" if prev_time is not None else ""
            closer = _closer_look(att, m.key)
            if closer:
                issues.append(Issue("unknown", "unknown",
                                    f"Couldn't verify: {m.text} — the first reading missed this step, but "
                                    f"{_closer_look_phrase(closer, a, rel, b)}.", prev_time, [a, b],
                                    m.critical, m.reason, m.quote, "Review this moment in the recording."))
            else:
                issues.append(Issue("skipped", "fail", f"Skipped: {m.text}{hint}", prev_time, [a, b],
                                    m.critical, m.reason, m.quote, f"{imperative(m.text)}.", list(m.key)))

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
    # The tracker can send an order to review, but it never fails anyone on its own.
    checks = order_checks(ref, att, proc, first, back) if tracked else []
    disputed = {c["id"] for c in checks if c["tracker"] == "conflicts"}
    broken = [r for r in broken if pair_id(r.before, r.after) not in disputed]
    for c in checks:
        if c["tracker"] == "conflicts":
            issues.append(Issue("doubt", "unknown",
                                f"Check the order: the video reading and the tracker disagree about whether "
                                f"{_q(c['before'])} came before {_q(c['after'])}.",
                                min(first[tuple(c["key_before"])], first[tuple(c["key_after"])]),
                                [c["key_before"][1], c["key_after"][1]],
                                fix="Review this part of the recording or re-record with both objects in view."))
    issues += _order_issues(broken, first, info)
    issues += undone_issues(ref, att, proc, mapping)

    for t, k, s in evs:
        if k not in ms_keys and k not in explained and k not in entailed:
            issues.append(Issue("extra", "warn", f"Extra step not in the reference: {k[1].replace('_', ' ')} "
                                f"{where(k[0], k[2])}", t, [k[1]]))

    status = []
    milestone_evidence: dict[Key, str | None] = {}
    for m in ms:
        done = m.key in first or m.key in finals
        ev = None
        if tracked and done:
            ev = _evidence(m.key, step_of[m.key], att, back) if m.key in step_of else "unseen"
            failed = [k for k in m.key[1:] if back.get(k, k) in att.meta.tracking_errors]
            moved = objs.get(back.get(m.key[1], m.key[1]))
            if (not failed and ev in ("confirmed", "seen") and m.key in step_of and moved is not None
                    and never_moved(moved.track, step_of[m.key])):
                ev = "still"
                issues.append(Issue("doubt", "unknown",
                                    f"Check this step: the video reading reports it, but the tracker shows the "
                                    f"{nice(m.key[1])} staying in one place.", first.get(m.key), [m.key[1], m.key[2]],
                                    m.critical, fix="Review this moment or re-record with the move in view."))
            elif failed:
                ev = "unseen"
                shared = [k for k in failed if back.get(k, k) in duplicates]
                issues.append(Issue("unknown", "unknown",
                                    (f"Couldn't verify: {m.text} — the tracker confused "
                                     + ", ".join(nice(k) for k in shared) + " with another object."
                                     if shared else f"Couldn't verify: {m.text} — tracking was unavailable for "
                                     + ", ".join(nice(k) for k in failed) + "."), first.get(m.key),
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
        milestone_evidence[m.key] = ev
        status.append({"text": m.text, "done": done, "time": first.get(m.key), "ref_time": m.ref_time,
                       "critical": m.critical, "reason": m.reason, "quote": m.quote, "evidence": ev})

    # Keep intermediate milestones, but independently enforce the terminal state.
    # A reached milestone is not permanent: later placements can invalidate it.
    final_states = []
    if final:
        required = set(proc.final_goals) if proc.final_goals is not None else final_keys(ref, {})
        ref_last = {k[1]: k for _, k, _ in events(ref, {})}
        if proc.final_goals is None:
            required |= {k for a, k in ref_last.items() if not any(r[1] == a for r in required)}
        latest = {k[1]: (t, k, s) for t, k, s in evs}
        locations = {a: item[1] for a, item in latest.items()}
        end = SimpleNamespace(end_s=max(0.0, att.video.duration_s - 1.5))
        final_evidence = {}
        if tracked:
            for key in required:
                final_evidence[key] = ("unseen" if any(back.get(a, a) in att.meta.tracking_errors for a in key[1:])
                                       else _evidence(key, end, att, back))
        for key in sorted(required):
            if key not in first and key not in finals:
                continue  # The missing milestone already explains this result.
            last = latest.get(key[1])
            conflicting = (last is not None and last[1] != key
                           and not (key[0] == "inside" and _inside_container(last[1], key[2], locations)))
            explicit_conflict = any(k[1] == key[1] and k[0] == key[0] and k != key for k in finals)
            ev = None
            if tracked:
                ev = final_evidence[key]
                if ev == "unseen" and not (conflicting or explicit_conflict) and _is_covered(
                        key, final_evidence, required, step_of, milestone_evidence, objs, back,
                        att.meta.tracking_errors):
                    ev = "covered"
            final_states.append({"key": list(key), "evidence": ev, "holds": not (conflicting or explicit_conflict)})
            if conflicting or explicit_conflict:
                coarse = last is not None and last[1][0] == "inside" and _inside_container(key, last[1][2], locations)
                closer = _closer_look(att, key)
                severity = "unknown" if coarse or closer or (tracked and ev != "contradicted") else "fail"
                issues.append(Issue("unknown" if severity == "unknown" else "wrong_place", severity,
                    (f"Couldn't verify the final placement of the {nice(key[1])} {where(key[0], key[2])}."
                     if coarse else f"Final placement changed: the {nice(key[1])} must finish {where(key[0], key[2])}."),
                    last[0] if last else None, list(key[1:]),
                    fix=f"Move the {nice(key[1])} {into(key[0], key[2])} and show the final result.",
                    check_key=list(key), reported_key=list(last[1]) if last else None))
            elif tracked and ev in ("contradicted", "unseen"):
                issues.append(Issue("unknown", "unknown", f"Couldn't verify the final placement of the {nice(key[1])}.",
                    last[0] if last else None, list(key[1:]), fix="Keep the final arrangement visible for review."))

    # A final picture cannot establish the order of an untimed placement.
    untimed = {k for k in ms_keys if k in finals and k not in first}
    for k in untimed:
        if any(r.strength == "required" and k in (r.before, r.after) for r in enforced):
            issues.append(Issue("unknown", "unknown", f"Couldn't verify when this step happened: {info[k].text}.",
                                None, [k[1]], info[k].critical,
                                fix="Show the full sequence so the required order can be checked."))

    # Conflicting identities cannot establish a skip, wrong placement or order failure.
    # Independent problems involving other objects are retained.
    for issue in issues:
        if issue.severity == "fail" and conflicts.keys() & set(issue.objects):
            issue.severity = "unknown"
            issue.kind = "identity"
            issue.message = "Object identity needs review before this problem can be confirmed: " + issue.message
    issues.sort(key=lambda i: (i.time is None, i.time or 0.0))
    sev = {i.severity for i in issues}
    verdict = "fail" if "fail" in sev else "unsure" if "unknown" in sev else "pass_notes" if sev else "pass"
    corroborated = verdict in ("pass", "pass_notes") and bool(status) and all(
        m["done"] and m["evidence"] == "confirmed" for m in status) and all(
        s["holds"] and s["evidence"] in ("confirmed", "covered") for s in final_states)
    evidence_status = ("inconclusive" if "unknown" in sev else "corroborated" if corroborated
                       else "partial" if tracked else "model_only")
    return Report(passed=verdict in ("pass", "pass_notes"), verdict=verdict, milestones=status, issues=issues,
                  mapping=mapping, rules=[rule_view(r, proc) for r in proc.shown()], verified=corroborated,
                  tracking_used=tracked, evidence_status=evidence_status, final_states=final_states,
                  order_checks=checks, second_looks=att.meta.second_looks)


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
    known = [o.reference_hint() for o in guide.objects] if guide else None
    g = build_task_graph(path, provider=provider, known_objects=known, track=track, spoken=spoken)
    out = ROOT / "runs" / f"{path.stem}.graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(g.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"read {path.name} -> {out.name}")
    return g


def load_turns(reference: Path) -> list[Turn] | None:
    """What was said in the reference, cached as runs/<stem>.turns.json."""
    from .clients import ROOT
    from .media import source_digest
    cache = ROOT / "runs" / f"{_stem(reference)}.{source_digest(reference)[:20]}.turns.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
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
    import hashlib
    identity = json.dumps({"graph": ref.model_dump(), "turns": [asdict(t) for t in turns or []], "version": 2}, sort_keys=True)
    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
    cache = ROOT / "runs" / f"{_stem(reference)}.{key}.narration.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
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
