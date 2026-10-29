"""Fresh generated-video check with labels and checker hash fixed before inference.

Run: python probes/judging_eval.py --per-kind 2 --seed 73129
Every case, failure and abstention stays in the denominator. Never tune the checker
against these results and continue describing this batch as held-out.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import ROOT
from make_pack_clip import EXPECT, make
from pack_eval import KINDS, correct

sys.path.insert(0, str(ROOT))
from oneshot.check import compare
from oneshot.graph import build_task_graph
from oneshot.procedure import Procedure
from oneshot.schema import TaskGraph
from web.build_judging import checker_digest
from web.views import report_view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-kind", type=int, default=2)
    parser.add_argument("--seed", type=int, default=73129)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "runs/judging_eval")
    args = parser.parse_args()
    if args.per_kind < 1:
        parser.error("--per-kind must be at least one")
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    plan_file = out / "plan.json"
    plan = {"checker_sha256": checker_digest(), "seed": args.seed,
            "scope": "New generated clips from the existing renderer; not new real-world tasks.",
            "cases": [{"id": f"judge_{kind}_{n}", "kind": kind, "seed": args.seed + 31 * k + n,
                       "expected": EXPECT[kind]} for k, kind in enumerate(KINDS) for n in range(args.per_kind)]}
    if plan_file.exists():
        if json.loads(plan_file.read_text(encoding="utf-8")) != plan:
            raise SystemExit("The frozen plan or checker changed. Use a new evaluation directory for a new batch.")
    else:
        plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    ref = TaskGraph.model_validate_json((ROOT / "web/reference.graph.json").read_text(encoding="utf-8"))
    proc = Procedure.from_dict(json.loads((ROOT / "web/reference.procedure.json").read_text(encoding="utf-8")))
    known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in ref.objects]
    results = []
    for case in plan["cases"]:
        if checker_digest() != plan["checker_sha256"]:
            raise SystemExit("Checker changed during evaluation. Results must be treated as development data.")
        cache = out / f"{case['id']}.graph.json"
        row = dict(case)
        try:
            if cache.exists():
                att = TaskGraph.model_validate_json(cache.read_text(encoding="utf-8"))
            else:
                if sum(r.get("cost_usd", 0) or 0 for r in results) >= 1.0:
                    raise RuntimeError("Evaluation's $1 model-cost limit reached")
                clip = out / f"{case['id']}.mp4"
                if not clip.exists(): make(case["kind"], clip, seed=case["seed"])
                print(case["id"], "reading fresh video", flush=True)
                att = build_task_graph(clip, work_dir=out, known_objects=known, track=case["kind"] == "hidden")
                cache.write_text(att.model_dump_json(), encoding="utf-8")
            report = compare(ref, att, procedure=proc)
            row.update(correct=correct(case["kind"], report), cost_usd=(att.meta.extract_cost_usd or 0) +
                       (0.10 if case["kind"] == "hidden" else 0), result=report_view(report, att))
        except Exception as exc:
            row.update(correct=False, error=f"{type(exc).__name__}: {str(exc)[:250]}")
        results.append(row)
        result = {"plan": plan, "results": results,
                  "execution_notes": "Checker rules stayed fixed. Network deadlines were added after an occlusion request stalled; completed fresh reads were reused and every failed case is retained.",
                  "summary": {"total": len(plan["cases"]), "completed": len(results),
                              "matched_expected": sum(r["correct"] for r in results),
                              "errors": sum("error" in r for r in results),
                              "abstentions": sum(r.get("result", {}).get("verdict") == "unsure" for r in results)}}
        (out / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(case["id"], row.get("result", {}).get("verdict", row.get("error")), "matched", row["correct"], flush=True)
    (ROOT / "web/static/media/judging_eval.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"]), flush=True)


if __name__ == "__main__":
    main()
