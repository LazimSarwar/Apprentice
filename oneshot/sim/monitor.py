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

from dataclasses import dataclass, field

from ..schema import TaskGraph

RelKey = tuple[str, str, str]      # (relation, a, b)


def rel_key(r) -> RelKey:
    return (r.relation, r.a, r.b)


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
