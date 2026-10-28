"""Measure the upgraded checker on randomized pack-out attempts.

Every attempt is a freshly rendered clip (random start positions and timing) of one kind from
`make_pack_clip.py`, read by the real extractor (guided by the reference) and checked against
the procedure learned from the narrated reference plus the second correct reference. Expected
outcome per kind (written before running):

  ok, ok_alt   pass                 (ok_alt does card before charger: a free order)
  loose        pass with a note     (charger before product: only shown, never said)
  order        fail, wrong order    ("liner first")
  skip_liner   fail, skipped
  skip_card    fail, skipped
  wrong_place  fail, wrong place
  hidden       no failure claimed   (a hand covers the box: "can't verify", never skipped/wrong place)

Usage:  python probes/pack_eval.py [--per-kind 3] [--seed 100]
        needs runs/pack_ref.graph.json + runs/pack.procedure.json (from `python -m oneshot.check`)
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from common import ROOT
from make_pack_clip import EXPECT, make

sys.path.insert(0, str(ROOT))
from oneshot.check import compare  # noqa: E402
from oneshot.graph import build_task_graph  # noqa: E402
from oneshot.procedure import Procedure  # noqa: E402
from oneshot.schema import TaskGraph  # noqa: E402

KINDS = ["ok", "ok_alt", "loose", "order", "skip_liner", "skip_card", "wrong_place", "hidden"]


def correct(kind: str, report) -> bool:
    fails = {i.kind for i in report.issues if i.severity == "fail"}
    want = EXPECT[kind]
    if want == "pass":
        return report.verdict == "pass"
    if want == "pass_notes":
        return report.verdict == "pass_notes" and any(i.kind == "order" for i in report.issues)
    if want == "no_false_fail":                # it couldn't see: "can't verify" or a pass, never a failure
        return report.verdict in ("unsure", "pass", "pass_notes")
    return report.verdict == "fail" and want in fails


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--per-kind", type=int, default=3)
    p.add_argument("--seed", type=int, default=100)
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    runs = ROOT / "runs"
    ref = TaskGraph.model_validate_json((runs / "pack_ref.graph.json").read_text(encoding="utf-8"))
    proc = Procedure.from_dict(json.loads((runs / "pack.procedure.json").read_text(encoding="utf-8")))
    known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in ref.objects]
    out_dir = runs / "pack_eval"
    results = []
    for k_i, kind in enumerate(KINDS):
        for n in range(args.per_kind):
            stem = f"packeval_{kind}_{n}"
            cache = out_dir / f"{stem}.graph.json"
            start = time.perf_counter()
            if cache.exists():
                att = TaskGraph.model_validate_json(cache.read_text(encoding="utf-8"))
            else:
                clip = make(kind, out_dir / f"{stem}.mp4", seed=args.seed + 31 * k_i + n)
                att = build_task_graph(clip, work_dir=out_dir, known_objects=known, track=(kind == "hidden"))
                cache.write_text(att.model_dump_json(), encoding="utf-8")
            report = compare(ref, att, procedure=proc)
            ok = correct(kind, report)
            kinds = sorted({i.kind for i in report.issues})
            results.append({"kind": kind, "n": n, "verdict": report.verdict, "issues": kinds, "correct": ok,
                            "messages": [i.message for i in report.issues],
                            "seconds": round(time.perf_counter() - start, 1), "cost": att.meta.extract_cost_usd})
            print(f"{kind:11} #{n}: {report.verdict:10} {kinds}  -> {'ok' if ok else 'WRONG'}", flush=True)

    print("\nresults")
    for kind in KINDS:
        rs = [r for r in results if r["kind"] == kind]
        print(f"  {kind:11} {sum(r['correct'] for r in rs)}/{len(rs)}   expected: {EXPECT[kind]}")
    total = sum(r["correct"] for r in results)
    print(f"  total       {total}/{len(results)}   extract cost ${sum(r['cost'] or 0 for r in results):.3f}")
    (runs / "pack_eval.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
