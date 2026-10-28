"""Near-live checking: warn about a mistake while the attempt is still going on.

The attempt is read again every few seconds — each time only the video so far — and every
read is judged for what is *already* certainly wrong. A skip can't be called until the attempt
ends, but some mistakes are final the moment they happen:

  order        — a step done while a step that must come first isn't done yet, or two steps
                 done in a required order the wrong way round (as-shown orders are notes)
  wrong_place  — an object put somewhere the procedure never puts it

Reads disagree a little from one to the next, so alerts are kept across reads: a wrong place
is *fixed* once the object reaches its right place, and an alert that two later reads in a row
no longer see is *withdrawn* rather than silently dropped. When the attempt ends, the whole
video gets the usual full check (`check.compare`), which is the verdict.

Run (replays a finished video as if it were live, reading at each step):
    python -m oneshot.watch REFERENCE_GRAPH ATTEMPT_VIDEO [--every 3]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field

from .check import Issue, Report, compare
from .procedure import Key, Procedure, first_times, learn, map_objects
from .schema import TaskGraph
from .words import clock, imperative

SETTLE = 0.5           # a step must have ended this long before the end of the read to count
WITHDRAW_AFTER = 2     # reads in a row that no longer see an alert before it is withdrawn


@dataclass
class Alert:
    id: str
    kind: str              # order | wrong_place
    severity: str          # fail | warn
    message: str
    time: float | None     # when it happened, seconds into the attempt
    seen_at: float         # how much of the attempt had been read when it was first caught
    objects: list[str] = field(default_factory=list)
    critical: bool = False
    reason: str | None = None
    quote: str | None = None
    fix: str | None = None
    status: str = "open"   # open | fixed | withdrawn
    closed_at: float | None = None
    misses: int = 0


def _id(issue: Issue) -> str:
    """Stable across reads: a wrong place by its object, an order problem by its late step."""
    return f"{issue.kind}{'-note' if issue.severity == 'warn' else ''}:{issue.objects[0]}"


def _q(text: str) -> str:
    return f"“{text}”"


def so_far(att: TaskGraph, upto: float) -> TaskGraph:
    """The attempt as read up to `upto` seconds: steps that hadn't settled yet are left out
    (an object still in the hand isn't a placement), and so are the final relations, which
    describe a video that isn't over."""
    steps = [s for s in att.steps if s.end_s <= upto - SETTLE]
    return att.model_copy(update={"steps": steps, "final_relations": []})


def reached(ref: TaskGraph, att: TaskGraph, proc: Procedure) -> dict[Key, float]:
    """Milestone -> when the attempt first reached it."""
    keys = {s.key for s in proc.steps}
    return {k: t for k, t in first_times(att, map_objects(ref, att)).items() if k in keys}


def wrong_now(ref: TaskGraph, att: TaskGraph, proc: Procedure) -> list[Issue]:
    """What is already certainly wrong in a partial attempt (`att` cut with `so_far`).

    Order problems are grouped on the step that is missing or late ("the liner isn't in yet,
    but the charger and the card are"), so one root cause stays one alert as more steps pile
    up behind it, and keeps its identity when the late step finally happens."""
    out = [i for i in compare(ref, att, procedure=proc).issues if i.kind == "wrong_place"]
    misplaced = {i.objects[0] for i in out}
    first = reached(ref, att, proc)

    late: dict[tuple[str, Key], list[Key]] = {}       # (level, late step) -> steps done before it
    for r in proc.enforced():
        if r.after not in first or r.after[1] in misplaced or r.before[1] in misplaced:
            continue                                   # a wrong place already explains it
        if r.before not in first or first[r.before] > first[r.after]:
            if r.strength == "as_shown" and r.before not in first:
                continue                               # only a note, and maybe it is still coming
            late.setdefault(("required" if r.strength == "required" else "note", r.before), []).append(r.after)
    failing = {k[1] for (level, key), afters in late.items() if level == "required" for k in (key, *afters)}
    for (level, key), afters in sorted(late.items(), key=lambda kv: kv[0][0] != "required"):
        if level == "note" and (key[1] in failing or any(a[1] in failing for a in afters)):
            continue                                   # a step already failing on order needs no note
        step, done = proc.step(key), [proc.step(a) for a in sorted(afters, key=first.get)]
        names = " and ".join(_q(d.text) for d in done)
        objs = [key[1], *[a[1] for a in afters]]
        when = min(first[a] for a in afters)
        if level == "note":
            out.append(Issue("order", "warn", f"Different order from the reference: {_q(step.text)} "
                             f"happened after {names}", when, objs, False,
                             "Nothing says this order matters. Mark it as required if it does."))
            continue
        pending = key not in first
        msg = (f"{names} happened, but {_q(step.text)} hasn't been done yet" if pending
               else f"Wrong order: {_q(step.text)} happened after {names}")
        out.append(Issue("order", "fail", msg, when, objs,
                         step.critical or any(d.critical for d in done),
                         step.reason or next((d.reason for d in done if d.reason), None),
                         step.quote, f"{imperative(step.text)} first." if pending
                         else f"{imperative(step.text)} earlier, before {names}."))
    return out


class Watch:
    """Alerts kept across successive reads of one attempt."""

    def __init__(self, ref: TaskGraph, proc: Procedure):
        self.ref, self.proc = ref, proc
        self.alerts: dict[str, Alert] = {}
        self.upto = 0.0
        self.progress: dict[Key, float] = {}

    def update(self, att: TaskGraph, upto: float) -> bool:
        """Fold in a read of the first `upto` seconds. Reads that finish out of order are
        ignored once a longer one has been folded in. Returns whether it was used."""
        if upto <= self.upto:
            return False
        self.upto = upto
        part = so_far(att, upto)
        self.progress = reached(self.ref, part, self.proc)
        now = {_id(i): i for i in wrong_now(self.ref, part, self.proc)}
        for aid, i in now.items():
            old = self.alerts.get(aid)
            if old is None:
                self.alerts[aid] = Alert(aid, i.kind, i.severity, i.message, i.time, upto, i.objects,
                                         i.critical, i.reason, i.quote, i.fix)
            else:                              # same problem, possibly grown: keep when it was caught
                old.message, old.time, old.objects, old.fix = i.message, i.time, i.objects, i.fix
                old.critical, old.reason, old.quote, old.misses = i.critical, i.reason, i.quote, 0
                if old.status == "withdrawn":
                    old.status, old.closed_at = "open", None
        for aid, a in self.alerts.items():
            if aid in now or a.status != "open":
                continue
            if a.kind == "wrong_place" and any(k[1] == a.objects[0] for k in self.progress):
                a.status, a.closed_at = "fixed", upto
                continue
            a.misses += 1
            if a.misses >= WITHDRAW_AFTER:
                a.status, a.closed_at = "withdrawn", upto
        return True

    def view(self) -> dict:
        return {"upto": self.upto,
                "alerts": sorted((asdict(a) for a in self.alerts.values()),
                                 key=lambda a: (a["seen_at"], a["time"] or 0.0)),
                "progress": [{"text": s.text, "critical": s.critical, "done": s.key in self.progress,
                              "time": self.progress.get(s.key)} for s in self.proc.steps]}

    def finish(self, att: TaskGraph) -> Report:
        """The verdict on the whole attempt."""
        return compare(self.ref, att, procedure=self.proc)


# --- CLI -----------------------------------------------------------------------------

def main() -> None:
    import json
    from pathlib import Path

    from .clients import ROOT
    from .graph import build_task_graph
    from .media import cut, video_info

    p = argparse.ArgumentParser(description="Replay a finished attempt as if it were live")
    p.add_argument("reference", type=Path, help="reference graph JSON")
    p.add_argument("attempt", type=Path, help="attempt video")
    p.add_argument("--procedure", type=Path, help="learned procedure JSON (default: learn from the reference)")
    p.add_argument("--every", type=float, default=3.0, help="read the video so far every N seconds")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    ref = TaskGraph.model_validate_json(args.reference.read_text(encoding="utf-8"))
    proc = (Procedure.from_dict(json.loads(args.procedure.read_text(encoding="utf-8")))
            if args.procedure else learn([ref]))
    known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in ref.objects]
    work = ROOT / "runs" / "watch"
    duration = video_info(args.attempt).duration_s
    w = Watch(ref, proc)
    t = args.every
    while True:
        upto = min(t, duration)
        if upto >= duration:                   # the whole video: the full check, at full effort
            att = build_task_graph(args.attempt, work_dir=work, known_objects=known, track=False)
            break
        clip = cut(args.attempt, upto, work / f"{args.attempt.stem}_{int(upto * 10):04d}.mp4", fps=5)
        att = build_task_graph(clip, work_dir=work, known_objects=known, track=False, effort="minimal")
        w.update(att, upto)
        print(f"-- read 0:00-{clock(upto)} ({att.meta.extract_seconds}s)")
        for a in w.view()["alerts"]:
            print(f"   [{a['status']:9}] {clock(a['time'])}  {a['message']}")
        t += args.every
    print(f"\n=== whole video ===\n{w.finish(att).summary()}")


if __name__ == "__main__":
    main()
