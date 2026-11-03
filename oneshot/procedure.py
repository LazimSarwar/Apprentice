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
from .semantics import is_container as _is_container
from .words import SURFACE, goal_past, nice, step_text

Key = tuple[str, str, str]                     # (relation, object, target)

STRENGTHS = ("required", "free", "as_shown")
_NEEDS_TARGET = ("place", "stack", "push")
_SURFACE_WORDS = ("table", "desk", "surface", "mat", "floor", "counter", "bench", "worktop", "tabletop")


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
    final_goals: list[Key] | None = None
    object_kinds: dict[str, str] = field(default_factory=dict)

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
        return Procedure(self.steps, pairs, self.references, self.narration, self.final_goals, self.object_kinds)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["shown"] = [pair_id(r.before, r.after) for r in self.shown()]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Procedure":
        steps = [StepInfo(**{**s, "key": tuple(s["key"])}) for s in d["steps"]]
        pairs = [Rule(**{**r, "before": tuple(r["before"]), "after": tuple(r["after"])}) for r in d["pairs"]]
        return cls(steps, pairs, d.get("references", 1), d.get("narration"),
                   [tuple(k) for k in d["final_goals"]] if d.get("final_goals") is not None else None,
                   d.get("object_kinds") or {})


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


def _is_surface(obj) -> bool:
    if getattr(obj, "kind", None) is not None:
        return obj.kind == "surface"
    text = f"{obj.id} {obj.shape or ''}".lower()
    return any(word in text for word in _SURFACE_WORDS) and not _is_container(obj)


def canonical_steps(graph: TaskGraph) -> list:
    """The graph's steps with one canonical target each. Video models name the work surface
    inconsistently — "on the brown table" in one read, no target at all in the next — so both
    become a put-down on `SURFACE`. A target-less step first takes its target from a final
    relation about the same object (the model saw where it ended up, it just didn't say so in
    the step); a put-down only counts when the object isn't placed somewhere else afterwards.
    Steps that still have no target (a pick, a push to nowhere) are left as they are and the
    callers skip them."""
    surfaces = {o.id for o in graph.objects if _is_surface(o)}
    steps = sorted(graph.steps, key=lambda s: s.order)
    out = []
    for i, s in enumerate(steps):
        target, rel = s.target, s.relation
        if target in surfaces:
            target, rel = SURFACE, "on"
        elif not target and s.action in _NEEDS_TARGET:
            later = any(t.object == s.object and t.target and t.target not in surfaces for t in steps[i + 1:])
            final = next((r for r in graph.final_relations if r.a == s.object and r.b not in surfaces), None)
            if later:
                continue
            if final is not None:
                target, rel = final.b, rel or final.relation
            elif s.action != "push" and rel in (None, "on"):
                target, rel = SURFACE, "on"
            else:
                continue
        out.append(s.model_copy(update={"target": target, "relation": rel}))
    return out


def final_keys(graph: TaskGraph, mapping: dict[str, str]) -> set[Key]:
    """The graph's final relations as milestone keys in reference ids, canonical like the steps."""
    objs = {o.id: o for o in graph.objects}
    to_ref = lambda x: mapping.get(x, x)
    out: set[Key] = set()
    for r in graph.final_relations:
        b = objs.get(r.b)
        if b is not None and _is_surface(b):
            out.add(("on", to_ref(r.a), SURFACE))
        elif r.relation in ("on", "inside"):
            out.add(("inside" if b is not None and _is_container(b) else "on", to_ref(r.a), to_ref(r.b)))
        else:
            out.add((r.relation, to_ref(r.a), to_ref(r.b)))
    return out


def milestones(ref: TaskGraph) -> list[Milestone]:
    """Ordered, de-duplicated results of the reference's (canonical) steps."""
    out: list[Milestone] = []
    seen: set[Key] = set()
    for s in canonical_steps(ref):
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
    """Attempt -> reference identity, with unique appearance matches for renamed objects.

    Colour alone cannot turn an unfamiliar object into a required one. Fallback matches
    must be unique in both directions; object iteration order never resolves ambiguity.
    """
    ref_ids = {o.id for o in ref.objects if not _is_surface(o)}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for a in att.objects:
        if a.id in ref_ids:
            mapping[a.id] = a.id
            used.add(a.id)
    def description(o):
        return " ".join((o.tracking_description or "").casefold().split())

    def shape(o):
        value = " ".join((o.shape or "").casefold().split())
        return {"cube": "block", "cuboid": "block"}.get(value, value)

    def same_description(a, r):
        return bool(description(a)) and description(a) == description(r)

    def same_shape(a, r):
        # Rich descriptions that disagree may distinguish otherwise identical parts.
        if description(a) and description(r) and description(a) != description(r):
            return False
        return bool(_color(a) and shape(a)) and _color(a) == _color(r) and shape(a) == shape(r)

    for matches in (same_description, same_shape):
        candidates = {a.id: [r.id for r in ref.objects if r.id in ref_ids and r.id not in used
                            and _is_container(r) == _is_container(a) and matches(a, r)]
                      for a in att.objects if a.id not in mapping and not _is_surface(a)}
        for aid, ids in candidates.items():
            if len(ids) == 1 and sum(ids[0] in others for others in candidates.values()) == 1:
                mapping[aid] = ids[0]
                used.add(ids[0])
    return mapping


def events(graph: TaskGraph, mapping: dict[str, str]) -> list[tuple[float, Key, object]]:
    """(time, milestone key in reference ids, step) for every placing step, in time order."""
    to_ref = lambda x: mapping.get(x, x)
    out = []
    for s in sorted(canonical_steps(graph), key=lambda s: (s.end_s, s.order)):
        if s.target:
            out.append((s.end_s, (relation_of(s, graph), to_ref(s.object), to_ref(s.target)), s))
    return out


def with_confirmed_kinds(graph: TaskGraph, proc: Procedure) -> TaskGraph:
    """The graph with the solid/container/surface choices a person confirmed in the procedure."""
    if not proc.object_kinds:
        return graph
    return graph.model_copy(update={"objects": [o.model_copy(update={"kind": proc.object_kinds.get(o.id, o.kind)})
                                                for o in graph.objects]})


def with_reference_objects(att: TaskGraph, ref: TaskGraph, proc: Procedure) -> TaskGraph:
    """Add procedure objects the reading left out, so the tracker can look for them. A video
    model can omit something in plain view, and only a separate search can tell "still on the
    table" from "not in this recording". Appearance only: no steps or placements are added."""
    typed_ref = with_confirmed_kinds(ref, proc)
    named = {part for step in proc.steps for part in step.key[1:]}
    present = set(map_objects(typed_ref, with_confirmed_kinds(att, proc)).values()) | {o.id for o in att.objects}
    missing = [o for o in typed_ref.objects if o.id in named and o.id not in present and not _is_surface(o)]
    if not missing:
        return att
    added = [o.model_copy(update={"from_reference": True, "tracked": False, "track": None}) for o in missing]
    return att.model_copy(update={"objects": [*att.objects, *added]})


def placement_timeline(graph: TaskGraph, mapping: dict[str, str]) -> list[tuple[float, str, Key | None]]:
    """(time, object in reference ids, where it now is) in time order. None means it was
    picked up or moved with no destination reported, so it is no longer placed anywhere.
    A push with no target is left out: it nudges an object without saying it left."""
    to_ref = lambda x: mapping.get(x, x)
    placed = {s.order: s for s in canonical_steps(graph) if s.target}
    out: list[tuple[float, str, Key | None]] = []
    for s in sorted(graph.steps, key=lambda s: (s.end_s, s.order)):
        found = placed.get(s.order)
        if found is not None:
            out.append((s.end_s, to_ref(s.object), (relation_of(found, graph), to_ref(s.object), to_ref(found.target))))
        elif s.action != "push":
            out.append((s.end_s, to_ref(s.object), None))
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
    terminal = final_keys(main, {})
    last = {key[1]: key for _, key, _ in events(main, {})}
    terminal |= {key for obj, key in last.items() if not any(k[1] == obj for k in terminal)}
    return Procedure(steps, pairs, references=len(refs),
                     narration=narration.to_dict() if narration else None, final_goals=sorted(terminal))
