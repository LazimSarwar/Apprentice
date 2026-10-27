"""Temporal monitor: did the run follow the demonstrated *procedure*, not just reach
the right final picture?

The demonstration fixes an order in which the final relations are established (step 1
puts the base in place, step 2 stacks on it, ...). `derive_rules` reads that order off
the task graph as a set of "A must hold no later than B" ordering rules — the confirmed
procedure. `check_rules` replays an execution trace (what actually held after each step)
against those rules. Two runs can satisfy every final relation yet get different verdicts
here: same final picture, only one followed the procedure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schema import TaskGraph

RelKey = tuple[str, str, str]      # (relation, a, b)

_KEY_RE = re.compile(r"^\s*(\w+)\s*\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)\s*$")


def rel_key(r) -> RelKey:
    return (r.relation, r.a, r.b)


def key_to_str(k: RelKey) -> str:
    """A human-readable, round-trippable key, e.g. 'inside(red_block, gray_tray_left)'."""
    return f"{k[0]}({k[1]}, {k[2]})"


def str_to_key(s: str) -> RelKey:
    m = _KEY_RE.match(s)
    if not m:
        raise ValueError(f"not a 'relation(a, b)' key: {s!r}")
    return (m.group(1), m.group(2), m.group(3))


@dataclass
class Snapshot:
    """Which watched relations held just after a given point in the run."""

    step_order: int                # 0 = initial scene, then the order of each executed step
    action: str | None
    holds: dict[RelKey, bool] = field(default_factory=dict)


Trace = list[Snapshot]


@dataclass
class OrderingRule:
    before: RelKey
    after: RelKey
    note: str = "demonstrated order"


@dataclass
class RuleVerdict:
    before: RelKey
    after: RelKey
    note: str
    ok: bool
    detail: str


def _establishing_order(graph: TaskGraph, r) -> int | None:
    """The step order at which relation r is meant to be established."""
    cands = [s for s in graph.steps if s.object == r.a]
    exact = [s for s in cands if s.target == r.b]
    chosen = exact or cands
    return min((s.order for s in chosen), default=None)


def derive_rules(graph: TaskGraph) -> list[OrderingRule]:
    """Read the confirmed procedure off the demonstration: the order its steps
    establish the final relations becomes a chain of ordering rules."""
    ordered = sorted(
        ((_establishing_order(graph, r), r) for r in graph.final_relations),
        key=lambda pair: (pair[0] is None, pair[0] or 0),
    )
    seq = [r for order, r in ordered if order is not None]
    return [OrderingRule(before=rel_key(seq[i]), after=rel_key(seq[i + 1]))
            for i in range(len(seq) - 1)]


def first_true_step(trace: Trace, key: RelKey) -> int | None:
    for snap in trace:
        if snap.holds.get(key):
            return snap.step_order
    return None


def holds_at(trace: Trace, step_order: int, key: RelKey) -> bool:
    for snap in trace:
        if snap.step_order == step_order:
            return snap.holds.get(key, False)
    return False


def _fmt(k: RelKey) -> str:
    return f"{k[1]} {k[0]} {k[2]}"


def check_rule(trace: Trace, rule: OrderingRule) -> RuleVerdict:
    fa = first_true_step(trace, rule.after)
    fb = first_true_step(trace, rule.before)
    if fa is None:
        return RuleVerdict(rule.before, rule.after, rule.note, True,
                           f"'{_fmt(rule.after)}' never established — ordering not exercised")
    if fb is None or fb > fa:
        return RuleVerdict(rule.before, rule.after, rule.note, False,
                           f"'{_fmt(rule.after)}' was established before '{_fmt(rule.before)}'")
    if not holds_at(trace, fa, rule.before):
        return RuleVerdict(rule.before, rule.after, rule.note, False,
                           f"'{_fmt(rule.before)}' did not hold when '{_fmt(rule.after)}' was established")
    return RuleVerdict(rule.before, rule.after, rule.note, True,
                       f"'{_fmt(rule.before)}' held before '{_fmt(rule.after)}'")


def check_rules(trace: Trace, rules: list[OrderingRule]) -> list[RuleVerdict]:
    return [check_rule(trace, r) for r in rules]


# --- confirmable procedure spec (P2 productization) ----------------------------

def propose_spec(graph: TaskGraph) -> dict:
    """An editable spec of the ordering rules read off the demonstration.

    Every proposed rule starts `confirmed: false`: the user reviews them, flips the
    ones they accept to `confirmed: true`, edits `before`/`after`, adds or deletes, and
    only confirmed rules are then enforced. The relation keys that exist in the task are
    listed for reference.
    """
    rules = derive_rules(graph)
    return {
        "relations_in_task": [key_to_str(rel_key(r)) for r in graph.final_relations],
        "how_to_use": ("Set confirmed:true on each ordering rule you accept. Edit "
                       "before/after (use the relation keys above), add rules, or delete "
                       "them. Only confirmed rules are enforced by the monitor."),
        "rules": [
            {
                "before": key_to_str(r.before),
                "after": key_to_str(r.after),
                "means": f"{_fmt(r.before)}  must hold no later than  {_fmt(r.after)}",
                "source": r.note,
                "confirmed": False,
            }
            for r in rules
        ],
    }


def rules_from_spec(spec: dict) -> list[OrderingRule]:
    """The confirmed ordering rules from an (edited) spec, as OrderingRule objects."""
    out: list[OrderingRule] = []
    for i, r in enumerate(spec.get("rules", []), start=1):
        if not r.get("confirmed"):
            continue
        try:
            before, after = str_to_key(r["before"]), str_to_key(r["after"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"rule {i} in the procedure spec is malformed: {exc}") from exc
        out.append(OrderingRule(before=before, after=after, note=r.get("source", "confirmed")))
    return out


def validate_spec(spec: dict, graph: TaskGraph) -> list[str]:
    """Warnings for confirmed rules that reference objects not in the task."""
    known = {o.id for o in graph.objects}
    warnings: list[str] = []
    for i, r in enumerate(spec.get("rules", []), start=1):
        if not r.get("confirmed"):
            continue
        for field_name in ("before", "after"):
            try:
                _, a, b = str_to_key(r[field_name])
            except (KeyError, ValueError):
                continue
            for obj in (a, b):
                if obj not in known:
                    warnings.append(f"rule {i}: '{obj}' is not an object in the task")
    return warnings
