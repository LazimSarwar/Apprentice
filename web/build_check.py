"""Build the procedure-checker demo the web page shows.

One reference video and several attempts. For each video: the clip itself and the clip with
the tracker's boxes drawn on (what the AI saw). For each attempt: the checker's report against
the reference, with the moment each step happened and each problem to look at. Plus the
robot replaying the reference, and the measured accuracy from `probes/proc_eval.py`.

Uses the graphs already extracted by `python -m oneshot.check` (so the page shows exactly
those verdicts) and only adds tracking evidence, cached next to them.

Run from app/:  python -m web.build_check
"""

from __future__ import annotations

import json
from pathlib import Path

import imageio.v3 as iio

from oneshot.check import compare
from oneshot.graph import add_evidence
from oneshot.schema import TaskGraph
from oneshot.sim.executor import execute
from oneshot.words import goal_text, nice, step_text
from web.build_media import write_mp4, overlay_frames

APP = Path(__file__).resolve().parent.parent
RUNS = APP / "runs"
MEDIA = APP / "web" / "static" / "media"

REFERENCE = ("proc", "Sort three blocks into three trays — red, then blue, then green.")
ATTEMPTS = [
    ("proc_ok", "Done correctly", "Same procedure, but the blocks started in different places and moved at a different pace."),
    ("proc_swap", "Wrong order", "We placed the blue block before the red one."),
    ("proc_skip", "Skipped a step", "We never placed the green block."),
    ("proc_wrongbin", "Wrong place", "The red and blue blocks went into each other's trays."),
]
KIND = {"skipped": "Skipped step", "order": "Wrong order", "wrong_place": "Wrong place", "extra": "Extra step"}


def _graph(stem: str) -> TaskGraph:
    return TaskGraph.model_validate_json((RUNS / f"{stem}.graph.json").read_text(encoding="utf-8"))


def _clip(stem: str) -> Path:
    return RUNS / "clips" / f"{stem}.720p.mp4"


def _tracked(stem: str) -> TaskGraph:
    cache = RUNS / f"{stem}.tracked.graph.json"
    if cache.exists():
        return TaskGraph.model_validate_json(cache.read_text(encoding="utf-8"))
    print(f"[{stem}] tracking objects for evidence")
    g = add_evidence(_graph(stem), _clip(stem))
    cache.write_text(g.model_dump_json(indent=2), encoding="utf-8")
    return g


def _videos(stem: str) -> dict:
    clip = _clip(stem)
    fps = float(iio.immeta(clip).get("fps", 30))
    print(f"[{stem}] videos")
    write_mp4(MEDIA / f"{stem}_demo.mp4", iio.imiter(clip), fps)
    write_mp4(MEDIA / f"{stem}_seen.mp4", overlay_frames(clip, _tracked(stem)), fps)
    return {"demo": f"media/{stem}_demo.mp4", "seen": f"media/{stem}_seen.mp4"}


def main() -> None:
    MEDIA.mkdir(parents=True, exist_ok=True)
    stem, blurb = REFERENCE
    ref = _graph(stem)
    tracked = _tracked(stem)
    out: dict = {
        "reference": {
            "blurb": blurb, **_videos(stem),
            "model": ref.meta.model,
            "steps": [{"text": step_text(s), "start": s.start_s, "end": s.end_s}
                      for s in sorted(ref.steps, key=lambda s: s.order)],
            "objects": [{"name": nice(o.id), "color": o.color, "tracked": bool(o.tracked)} for o in tracked.objects],
        },
        "attempts": [],
    }

    for a_stem, title, what in ATTEMPTS:
        report = compare(ref, _graph(a_stem))
        out["attempts"].append({
            "id": a_stem, "title": title, "what": what, **_videos(a_stem),
            "passed": report.passed,
            "milestones": report.milestones,
            "issues": [{"kind": KIND.get(i.kind, i.kind), "severity": i.severity,
                        "message": i.message, "time": i.time} for i in report.issues],
        })
        print(f"[{a_stem}] {'PASS' if report.passed else 'FAIL'} ({len(report.issues)} issue(s))")

    print("[robot] replaying the reference")
    r = execute(ref, record=True)
    write_mp4(MEDIA / "proc_robot.mp4", r.frames, fps=30)
    out["robot"] = {"video": "media/proc_robot.mp4", "success": bool(r.success),
                    "goals": [{"text": goal_text(f.relation, f.a, f.b), "ok": bool(f.holds)} for f in r.final]}

    evals = json.loads((RUNS / "proc_eval.json").read_text(encoding="utf-8"))
    acc = {}
    for kind in ("ok", "swap", "skip", "wrongbin"):
        rs = [e for e in evals if e["kind"] == kind]
        acc[kind] = [sum(e["correct"] for e in rs), len(rs)]
    out["accuracy"] = acc

    (MEDIA / "check.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    # the live checker on the web page compares uploads against this same reference
    (APP / "web" / "reference.graph.json").write_text(ref.model_dump_json(indent=2), encoding="utf-8")
    print("wrote", MEDIA / "check.json")


if __name__ == "__main__":
    main()
