"""Measure the procedure checker over many randomized attempt videos.

Each attempt is a freshly rendered clip of the three-bin procedure with random start
positions and timings, of one of four kinds:

  ok        — correct procedure          (should PASS — anything else is a false alarm)
  swap      — two adjacent steps swapped  (should be flagged as a different order; with one silent
                                           reference nothing says the order matters, so it is a note)
  skip      — one step never happens      (should be caught as skipped)
  wrongbin  — two blocks swap bins        (should be caught as wrong place)

Every attempt goes through the real extractor (guided by the reference) and the checker.
Seeded, so the set of videos is reproducible.

Usage:  python probes/proc_eval.py [--per-kind 5] [--seed 0]     # needs runs/proc.graph.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from common import ROOT
from make_test_clip import STARTS, bin_spot, render_moves

sys.path.insert(0, str(ROOT))
from oneshot.check import compare  # noqa: E402
from oneshot.graph import build_task_graph  # noqa: E402
from oneshot.schema import TaskGraph  # noqa: E402

ORDER = [("red", "left"), ("blue", "middle"), ("green", "right")]
EXPECT = {"ok": None, "swap": "order", "skip": "skipped", "wrongbin": "wrong_place"}


def variant(kind: str, rng: random.Random) -> dict:
    steps = list(ORDER)
    if kind == "swap":
        i = rng.randrange(2)
        steps[i], steps[i + 1] = steps[i + 1], steps[i]
    elif kind == "skip":
        i = rng.randrange(3)
        steps[i] = (steps[i][0], None)
    elif kind == "wrongbin":
        i, j = rng.sample(range(3), 2)
        (ci, bi), (cj, bj) = steps[i], steps[j]
        steps[i], steps[j] = (ci, bj), (cj, bi)
    # random start positions along the top, random but non-overlapping timing slots
    xs = rng.sample([120, 340, 560, 780, 1000], 3)
    t, moves = rng.uniform(0.4, 1.2), {}
    for (colour, bin_name), x in zip(steps, xs):
        dur = rng.uniform(1.6, 2.6)
        start = (x, rng.randint(50, 130))
        moves[colour] = (start, bin_spot(bin_name) if bin_name else None, round(t, 2), round(t + dur, 2))
        t += dur + rng.uniform(0.8, 1.6)
    return {"kind": kind, "steps": steps, "moves": moves, "duration": round(t + 0.8, 1)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--per-kind", type=int, default=5)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    ref = TaskGraph.model_validate_json((ROOT / "runs" / "proc.graph.json").read_text(encoding="utf-8"))
    known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in ref.objects]
    rng = random.Random(args.seed)
    out_dir = ROOT / "runs" / "proc_eval"
    results = []
    for kind in EXPECT:
        for n in range(args.per_kind):
            v = variant(kind, rng)
            clip = render_moves(out_dir / f"{kind}_{n}.mp4", v["moves"], duration=v["duration"])
            att = build_task_graph(clip, known_objects=known, track=False)
            report = compare(ref, att)
            kinds = sorted({i.kind for i in report.issues if i.severity == "fail"})
            want = EXPECT[kind]
            if want is None:
                correct = report.verdict == "pass"
            elif want == "order":            # one silent reference: a swap is flagged as a note, not a failure
                correct = any(i.kind == "order" for i in report.issues)
            else:
                correct = not report.passed and want in kinds
            results.append({"kind": kind, "n": n, "steps": v["steps"], "passed": report.passed,
                            "issues": kinds, "correct": correct,
                            "messages": [i.message for i in report.issues]})
            print(f"{kind:9} #{n}: {'PASS' if report.passed else 'FAIL'} {kinds}  -> {'ok' if correct else 'WRONG'}",
                  flush=True)

    print("\nresults")
    for kind, want in EXPECT.items():
        rs = [r for r in results if r["kind"] == kind]
        good = sum(r["correct"] for r in rs)
        what = "passed (no false alarm)" if want is None else f"caught as {want}"
        print(f"  {kind:9} {good}/{len(rs)} {what}")
    (ROOT / "runs" / "proc_eval.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
