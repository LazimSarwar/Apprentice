"""The procedure learned from one or more reference videos — not just a recording to diff.

A reference video gives ordered *milestones* (the result of each step: "the liner is in the
box"). What it can't tell you on its own is which parts of that order actually matter. Every
pair of milestones gets an ordering verdict, from the strongest evidence available:

  required  — the person *said* so ("liner first"), or it's *physical*: the product sits on the
              liner, so the liner must be in place first.
  free      — they said the order doesn't matter, or a second correct reference did it the
              other way round.
  as shown  — only seen in this order; nothing says it matters. A different order is reported
              as a note, never as a failure.

Narration also gives each step its reason ("so the product doesn't get scratched") and marks
critical steps. A person can then confirm or change any rule; their choice wins.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .narration import Narration
from .schema import TaskGraph
from .sim.executor import sanitize_plan
from .sim.scene import _is_container
from .words import goal_past, nice, step_text

Key = tuple[str, str, str]                     # (relation, object, target)

STRENGTHS = ("required", "free", "as_shown")


@dataclass
class Milestone:
    key: Key
    text: str              # the reference step, in plain English
    ref_time: float        # when it is reached in the reference video
    start: float = 0.0     # when the step starts in the reference video


@dataclass
class StepInfo:
    key: Key
    text: str
    ref_time: float
    start: float
    reason: str | None = None
    quote: str | None = None
    critical: bool = False


@dataclass
class Rule:
    before: Key
    after: Key
    strength: str          # required | free | as_shown
    source: str            # said | physical | references | shown | you
    why: str
    quote: str | None = None


@dataclass
class Procedure:
    steps: list[StepInfo]
    pairs: list[Rule]                          # one verdict per pair of milestones
    references: int = 1
    narration: dict | None = None

    def step(self, key: Key) -> StepInfo | None:
        return next((s for s in self.steps if s.key == key), None)

    def enforced(self) -> list[Rule]:
        return [r for r in self.pairs if r.strength != "free"]

    def shown(self) -> list[Rule]:
        """The rules worth showing a person: required ones without the ones they imply,
        every free pair, and as-shown pairs that no required chain already covers."""
        req = {(r.before, r.after) for r in self.pairs if r.strength == "required"}
        closure = _closure(req)
        keys = [s.key for s in self.steps]
        out = []
        for r in self.pairs:
            implied = any((r.before, m) in closure and (m, r.after) in closure
                          for m in keys if m not in (r.before, r.after))
            if r.strength == "required" and implied:
                continue
            if r.strength == "as_shown" and ((r.before, r.after) in closure or implied):
                continue
            out.append(r)
        return out

    def with_overrides(self, overrides: dict[str, str]) -> "Procedure":
        """Apply a person's choices: {"<before key> -> <after key>": strength}."""
        pairs = []
        for r in self.pairs:
            want = overrides.get(pair_id(r.before, r.after))
            if want in STRENGTHS and want != r.strength:
                why = {"required": "You marked this order as required.",
                       "free": "You marked this order as not mattering.",
                       "as_shown": "You reset this to the order shown."}[want]
                r = Rule(r.before, r.after, want, "you", why, r.quote)
            pairs.append(r)
        return Procedure(self.steps, pairs, self.references, self.narration)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["shown"] = [pair_id(r.before, r.after) for r in self.shown()]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Procedure":
        steps = [StepInfo(**{**s, "key": tuple(s["key"])}) for s in d["steps"]]
        pairs = [Rule(**{**r, "before": tuple(r["before"]), "after": tuple(r["after"])}) for r in d["pairs"]]
        return cls(steps, pairs, d.get("references", 1), d.get("narration"))


def pair_id(before: Key, after: Key) -> str:
    return f"{'/'.join(before)} -> {'/'.join(after)}"


def _closure(edges: set[tuple[Key, Key]]) -> set[tuple[Key, Key]]:
    out = set(edges)
    changed = True
    while changed:
        changed = False
        for a, b in list(out):
            for c, d in list(out):
                if b == c and (a, d) not in out:
                    out.add((a, d))
                    changed = True
    return out


# --- milestones ----------------------------------------------------------------------

_DEFAULT_REL = {"stack": "on"}


def relation_of(step, graph: TaskGraph) -> str | None:
    """The step's result, canonical: video models say "on the box" and "in the box" for the
    same placement, so on/inside become "inside" for containers and "on" for everything else."""
    rel = step.relation or _DEFAULT_REL.get(step.action)
    target = next((o for o in graph.objects if o.id == step.target), None)
    container = target is not None and _is_container(target)
    if rel in ("on", "inside"):
        return "inside" if container else "on"
    return rel or ("inside" if container else "near")


def milestones(ref: TaskGraph) -> list[Milestone]:
    """Ordered, de-duplicated results of the reference's (sanitised) steps."""
    steps, _, _ = sanitize_plan(ref)
    out: list[Milestone] = []
    seen: set[Key] = set()
    for s in sorted(steps, key=lambda s: s.order):
        if not s.target:
            continue
        key = (relation_of(s, ref), s.object, s.target)
        if key not in seen:
            seen.add(key)
            out.append(Milestone(key, step_text(s), s.end_s, s.start_s))
    return out


# --- matching one video's objects to the reference's ---------------------------------

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


def events(graph: TaskGraph, mapping: dict[str, str]) -> list[tuple[float, Key, object]]:
    """(time, milestone key in reference ids, step) for every placing step, in time order."""
    to_ref = lambda x: mapping.get(x, x)
    out = []
    for s in sorted(graph.steps, key=lambda s: (s.end_s, s.order)):
        if s.target:
            out.append((s.end_s, (relation_of(s, graph), to_ref(s.object), to_ref(s.target)), s))
    return out


def first_times(graph: TaskGraph, mapping: dict[str, str]) -> dict[Key, float]:
    first: dict[Key, float] = {}
    for t, k, _ in events(graph, mapping):
        first.setdefault(k, t)
    return first


# --- learning --------------------------------------------------------------------------

def _physical(a: Key, b: Key) -> bool:
    """b puts something on/in the object a moved — so a has to happen first."""
    return b[0] in ("on", "inside") and b[2] == a[1]


def learn(refs: list[TaskGraph], narration: Narration | None = None) -> Procedure:
    """The procedure from the first reference, refined by any further correct references
    and by what was said while recording the first one."""
    main, others = refs[0], refs[1:]
    ms = milestones(main)
    notes = {n.step: n for n in (narration.steps if narration else [])}
    steps = [StepInfo(m.key, m.text, m.ref_time, m.start,
                      reason=notes[i].reason if i in notes else None,
                      quote=notes[i].quote if i in notes else None,
                      critical=notes[i].critical if i in notes else False)
             for i, m in enumerate(ms, 1)]

    said_order = {}                            # (i, j) 1-based -> quote
    for o in (narration.order if narration else []):
        said_order[(o.before, o.after)] = o.quote
    said_free = {}
    for x, y, q in (narration.any_order if narration else []):
        said_free[(min(x, y), max(x, y))] = q
    other_times = [first_times(r, map_objects(main, r)) for r in others]

    pairs: list[Rule] = []
    for i in range(len(ms)):
        for j in range(i + 1, len(ms)):
            a, b = ms[i].key, ms[j].key
            ia, ib = i + 1, j + 1
            pa, pb = goal_past(a), goal_past(b)
            if _physical(a, b):
                q = said_order.get((ia, ib))
                pairs.append(Rule(a, b, "required", "said" if q else "physical",
                                  f"The {nice(b[1])} goes {'on' if b[0] == 'on' else 'in'} the {nice(a[1])}, "
                                  f"so {pa} first.", q))
            elif (ia, ib) in said_order:
                pairs.append(Rule(a, b, "required", "said", "Said while recording the reference.",
                                  said_order[(ia, ib)]))
            elif (ib, ia) in said_order:           # said the other way round from how it was shown
                pairs.append(Rule(b, a, "required", "said", "Said while recording the reference.",
                                  said_order[(ib, ia)]))
            elif (ia, ib) in said_free:
                pairs.append(Rule(a, b, "free", "said", "Said the order doesn't matter.", said_free[(ia, ib)]))
            elif any(a in t and b in t and t[b] < t[a] for t in other_times):
                pairs.append(Rule(a, b, "free", "references",
                                  "Another correct reference did these the other way round."))
            else:
                pairs.append(Rule(a, b, "as_shown", "shown",
                                  "Only seen in this order; nothing says the order matters."))
    return Procedure(steps, pairs, references=len(refs),
                     narration=narration.to_dict() if narration else None)
