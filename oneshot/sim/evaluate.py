"""Controlled evaluation of the executor + monitor, with honest denominators.

Re-running the vision pipeline per trial would cost API calls and tell us little, so the
harness fixes one task graph and varies the *world*: seeded random layouts, random
disturbances, reordered execution, and out-of-reach targets. It then reports, with full
denominators, four things the pitch claims:

  shuffle      — reaches the goal from a randomized starting layout
  recovery     — reaches the goal despite a mid-run shove
  procedure    — the temporal monitor flags an out-of-order run
  abstention   — refuses to claim success when a target is unreachable

Everything is seeded, so every number here is reproducible.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from pathlib import Path

from ..schema import TaskGraph
from .executor import Disturbance, execute
from .scene import _is_container

# Reachable sampling windows on the table (metres), kept inside the Panda's workspace.
BLOCK_X = (0.38, 0.52)
BLOCK_Y = (-0.22, 0.22)
TRAY_X = (0.55, 0.68)
TRAY_Y = (-0.16, 0.16)
MIN_SEP = 0.13          # keep objects from starting on top of each other
FAR_TRAY_X = (1.3, 1.5)  # deliberately out of reach, for the abstention test


def _sample(rng: random.Random, xr, yr, taken: list[tuple[float, float]]) -> tuple[float, float]:
    for _ in range(300):
        x, y = rng.uniform(*xr), rng.uniform(*yr)
        if all((x - px) ** 2 + (y - py) ** 2 > MIN_SEP ** 2 for px, py in taken):
            taken.append((x, y))
            return round(x, 3), round(y, 3)
    taken.append((x, y))
    return round(x, 3), round(y, 3)


def random_layout(graph: TaskGraph, rng: random.Random, far_tray: bool = False) -> dict:
    blocks = [o for o in graph.objects if not _is_container(o)]
    trays = [o for o in graph.objects if _is_container(o)]
    taken: list[tuple[float, float]] = []
    places = {}
    for b in blocks:
        places[b.id] = _sample(rng, BLOCK_X, BLOCK_Y, taken)
    for t in trays:
        places[t.id] = _sample(rng, FAR_TRAY_X if far_tray else TRAY_X, TRAY_Y, taken)
    return places


@dataclass
class Metric:
    name: str
    hits: int
    total: int
    note: str = ""

    def line(self) -> str:
        pct = 100.0 * self.hits / self.total if self.total else 0.0
        return f"  {self.name:<12} {self.hits:>3}/{self.total:<3} ({pct:5.1f}%)  {self.note}"


def evaluate(graph: TaskGraph, trials: int = 15, seed: int = 0) -> list[Metric]:
    rng = random.Random(seed)
    metrics: list[Metric] = []

    # 1) Shuffle: random layout, in-order run. Goal reached + procedure followed.
    goal = proc = 0
    for _ in range(trials):
        r = execute(graph, layout=random_layout(graph, rng))
        goal += r.success
        proc += r.procedure_ok
    metrics.append(Metric("shuffle", goal, trials, "goal reached from a random layout"))
    metrics.append(Metric("procedure", proc, trials, "in-order run judged as following the procedure"))

    # 2) Recovery: random layout + a random shove after a random step.
    recovered = 0
    repairs_total = 0
    for _ in range(trials):
        layout = random_layout(graph, rng)
        step_orders = [s.order for s in graph.steps]
        dist = Disturbance(
            after_step=rng.choice(step_orders),
            object=rng.choice([o.id for o in graph.objects if not _is_container(o)]),
            dxy=(rng.uniform(-0.18, 0.18), rng.uniform(-0.18, 0.18)),
        )
        r = execute(graph, layout=layout, disturbances=[dist])
        recovered += r.success
        repairs_total += r.corrections
    metrics.append(Metric("recovery", recovered, trials,
                          f"recovered to goal after a shove (mean {repairs_total / trials:.1f} repairs)"))

    # 3) Procedure detection: reversed execution should be flagged as a violation.
    flagged = 0
    for _ in range(trials):
        layout = random_layout(graph, rng)
        order = sorted((s.order for s in graph.steps), reverse=True)
        r = execute(graph, layout=layout, order=order)
        flagged += (not r.procedure_ok)
    metrics.append(Metric("detection", flagged, trials, "out-of-order run flagged by the monitor"))

    # 4) Abstention: an unreachable target must not be reported as success.
    honest = 0
    for _ in range(trials):
        r = execute(graph, layout=random_layout(graph, rng, far_tray=True))
        honest += (not r.success)
    metrics.append(Metric("abstention", honest, trials, "no false success when the target is out of reach"))

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled evaluation of the executor + monitor")
    parser.add_argument("graph", type=Path, help="task graph JSON from `python -m oneshot`")
    parser.add_argument("--trials", type=int, default=15, help="trials per category")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    graph = TaskGraph.model_validate_json(args.graph.read_text(encoding="utf-8"))
    print(f"evaluating {args.graph.name}: {args.trials} trials/category, seed {args.seed}")
    for m in evaluate(graph, trials=args.trials, seed=args.seed):
        print(m.line())


if __name__ == "__main__":
    main()
