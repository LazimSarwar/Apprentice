"""Build the procedure-checker demo the web page shows: the pack-out procedure.

One narrated reference, a second correct reference, and eight attempts. The procedure is
learned from the two references and what was said (`oneshot.procedure.learn`); each attempt
is checked against it with the tracker's evidence. For each video: the clip (the reference
keeps its narration) and the clip with the tracker's boxes drawn on. Plus the robot replaying
the older three-tray reference, and the measured accuracy from `probes/pack_eval.py` and
`probes/proc_eval.py`.

Uses graphs already extracted by `python -m oneshot.check` and tracked ones cached as
`runs/<stem>.tracked.graph.json`. Writes `web/static/media/check.json`, the built-in reference
for the live checker (`web/reference.*.json`) and the tracked demo attempts (`web/demo/`).

Run from app/:  python -m web.build_check
"""

from __future__ import annotations

import json
from pathlib import Path

import imageio.v3 as iio

from oneshot.check import compare
from oneshot.graph import add_evidence
from oneshot.procedure import Procedure
from oneshot.schema import TaskGraph
from oneshot.sim.executor import execute
from oneshot.words import goal_text, nice
from web.build_media import overlay_frames, write_mp4
from web.views import add_audio, procedure_view, report_view

APP = Path(__file__).resolve().parent.parent
RUNS = APP / "runs"
MEDIA = APP / "web" / "static" / "media"
DEMO = APP / "web" / "demo"

REFERENCE = ("pack_ref", "Pack an order: liner into the box, product on the liner, charger in the box, "
             "thank-you card on top. Narrated while recording.")
SECOND = ("pack_ref2", "A second correct way: the card goes on before the charger.")
ATTEMPTS = [
    ("pack_ok", "Done correctly", "Items started in different places, packed at a different pace."),
    ("pack_ok_alt", "Different, still right", "Card before charger, like the second reference. That order is free."),
    ("pack_loose", "Unusual order", "Charger went in before the product. Nobody said that matters."),
    ("pack_order", "Liner too late", "The charger went in before the liner, but the liner has to go first."),
    ("pack_skip_liner", "No liner", "The product went straight into the box."),
    ("pack_skip_card", "No card", "The thank-you card was never added."),
    ("pack_wrong_place", "Wrong order box", "The charger went onto the other order's plate."),
    ("pack_hidden", "Hand in the way", "A hand covered the box before the card went in."),
]
ROBOT_REF = "proc"


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


def _videos(stem: str, audio_from: Path | None = None, seen: bool = True) -> dict:
    clip = _clip(stem)
    fps = float(iio.immeta(clip).get("fps", 30))
    print(f"[{stem}] videos")
    out = {}
    kinds = [("demo", lambda: iio.imiter(clip))] + ([("seen", lambda: overlay_frames(clip, _tracked(stem)))] if seen else [])
    for kind, frames in kinds:
        target = MEDIA / f"{stem}_{kind}.mp4"
        write_mp4(target, frames(), fps)
        if audio_from is not None:
            voiced = target.with_suffix(".voiced.mp4")
            add_audio(target, audio_from, voiced)
            voiced.replace(target)
        out[kind] = f"media/{stem}_{kind}.mp4"
    return out


def _accuracy(name: str) -> dict:
    path = RUNS / name
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    acc: dict[str, list[int]] = {}
    for r in rows:
        good, n = acc.get(r["kind"], [0, 0])
        acc[r["kind"]] = [good + bool(r["correct"]), n + 1]
    return acc


def main() -> None:
    MEDIA.mkdir(parents=True, exist_ok=True)
    DEMO.mkdir(parents=True, exist_ok=True)
    stem, blurb = REFERENCE
    ref = _graph(stem)
    proc = Procedure.from_dict(json.loads((RUNS / "pack.procedure.json").read_text(encoding="utf-8")))
    tracked = _tracked(stem)
    out: dict = {
        "reference": {
            "blurb": blurb, **_videos(stem, audio_from=APP / "clips" / f"{stem}.mp4"),
            "model": ref.meta.model, **procedure_view(proc),
            "objects": [{"name": nice(o.id), "color": o.color, "tracked": bool(o.tracked)} for o in tracked.objects],
        },
        "second": {"blurb": SECOND[1], "demo": _videos(SECOND[0], seen=False)["demo"]},
        "attempts": [],
    }
    for a_stem, title, what in ATTEMPTS:
        att = _tracked(a_stem)
        (DEMO / f"{a_stem}.graph.json").write_text(att.model_dump_json(), encoding="utf-8")
        report = compare(ref, att, procedure=proc)
        out["attempts"].append({"id": a_stem, "title": title, "what": what, **_videos(a_stem),
                                **report_view(report, att)})
        print(f"[{a_stem}] {report.verdict} ({len(report.issues)} issue(s))")

    print("[robot] replaying the three-tray reference")
    r = execute(_graph(ROBOT_REF), record=True)
    write_mp4(MEDIA / "proc_robot.mp4", r.frames, fps=30)
    out["robot"] = {"video": "media/proc_robot.mp4", "success": bool(r.success),
                    "goals": [{"text": goal_text(f.relation, f.a, f.b), "ok": bool(f.holds)} for f in r.final]}
    out["accuracy"] = {"pack": _accuracy("pack_eval.json"), "trays": _accuracy("proc_eval.json")}

    (MEDIA / "check.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    # the live checker compares uploads against this same reference and procedure
    (APP / "web" / "reference.graph.json").write_text(ref.model_dump_json(indent=2), encoding="utf-8")
    (APP / "web" / "reference.procedure.json").write_text(json.dumps(proc.to_dict(), indent=2), encoding="utf-8")
    print("wrote", MEDIA / "check.json")
    from web.build_judging import refresh
    if (RUNS / "real_ref.graph.json").exists() or (DEMO / "real_ref.graph.json").exists():
        refresh()


if __name__ == "__main__":
    main()
