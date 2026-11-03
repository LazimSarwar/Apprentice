"""What each layer adds, measured on the same saved video-model readings. No model calls.

Three checks read the same recordings:

  final picture  our simple comparison: accept if the required placements hold at the end.
                 It ignores order and never abstains. It is not a rival product.
  model reading  the full rule checker, using only what the video model reported.
  with tracking  the full rule checker plus object tracking (the shipped behaviour).

python -m web.baseline
"""
from __future__ import annotations

import json
from pathlib import Path

from oneshot.check import compare, holds_now
from oneshot.procedure import Procedure, final_keys, learn, map_objects, placement_timeline
from oneshot.schema import TaskGraph
from web.build_judging import APP, DEMO, MEDIA, pipeline_digest

# Attempt type -> what a correct check must do with it.
INCORRECT = {"order", "skip_liner", "skip_card", "wrong_place", "wrong_order", "skipped"}
CORRECT = {"ok", "ok_alt", "loose", "pass"}
OCCLUDED = {"hidden", "no_false_fail"}
SYSTEMS = ("final_picture", "model_reading", "with_tracking")


def _load(path: Path) -> TaskGraph:
    return TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))


def final_picture(ref: TaskGraph, att: TaskGraph, proc: Procedure) -> str:
    """Accept when every required final placement holds in the attempt's last state."""
    mapping = map_objects(ref, att)
    locations = {}
    for _, obj, key in placement_timeline(att, mapping):
        locations[obj] = key
    reported = final_keys(att, mapping)
    required = set(proc.final_goals) if proc.final_goals is not None else final_keys(ref, {})
    return "pass" if required and all(k in reported or holds_now(k, locations) for k in required) else "fail"


def model_only(att: TaskGraph) -> TaskGraph:
    """The same reading with every trace of tracking removed."""
    return att.model_copy(update={
        "objects": [o.model_copy(update={"track": None, "tracked": False}) for o in att.objects],
        "meta": att.meta.model_copy(update={"track_seconds": None, "tracking_errors": {}, "warnings": []})})


def _outcome(verdict: str) -> str:
    return "accept" if verdict in ("pass", "pass_notes") else "review" if verdict == "unsure" else "reject"


def _case(case_id: str, kind: str, ref: TaskGraph, att: TaskGraph, proc: Procedure) -> dict:
    return {"id": case_id, "kind": kind, "outcomes": {
        "final_picture": _outcome(final_picture(ref, att, proc)),
        "model_reading": _outcome(compare(ref, model_only(att), procedure=proc).verdict),
        "with_tracking": _outcome(compare(ref, att, procedure=proc).verdict)}}


def _summary(rows: list[dict]) -> dict:
    def share(kinds, outcome, system):
        return sum(r["outcomes"][system] == outcome for r in rows if r["kind"] in kinds)
    order = [r for r in rows if r["kind"] == "order"]
    return {"cases": len(rows), "incorrect": sum(r["kind"] in INCORRECT for r in rows),
            "correct": sum(r["kind"] in CORRECT for r in rows), "occluded": sum(r["kind"] in OCCLUDED for r in rows),
            "order_only": len(order), "systems": {s: {
                "incorrect_accepted": share(INCORRECT, "accept", s),
                "incorrect_rejected": share(INCORRECT, "reject", s),
                "incorrect_review": share(INCORRECT, "review", s),
                "order_only_accepted": sum(r["outcomes"][s] == "accept" for r in order),
                "correct_accepted": share(CORRECT, "accept", s),
                "correct_rejected": share(CORRECT, "reject", s),
                "correct_review": share(CORRECT, "review", s),
                "occluded_rejected": share(OCCLUDED, "reject", s),
                "occluded_review": share(OCCLUDED, "review", s),
                "occluded_accepted": share(OCCLUDED, "accept", s)} for s in SYSTEMS}}


def refresh() -> dict:
    pack_ref = _load(APP / "web/reference.graph.json")
    pack_proc = Procedure.from_dict(json.loads((APP / "web/reference.procedure.json").read_text(encoding="utf-8")))
    generated = []
    for path in sorted(DEMO.glob("pack_*.graph.json")):
        stem = path.name.removesuffix(".graph.json")
        generated.append(_case(stem, stem.removeprefix("pack_"), pack_ref, _load(path), pack_proc))
    repair = json.loads((MEDIA / "tracking_validation.json").read_text(encoding="utf-8"))
    for prior in repair["results"]:
        path = DEMO / "judging" / f"{prior['id']}.graph.json"
        if not path.exists():
            path = path.with_name(f"{prior['id']}.repaired.graph.json")
        generated.append(_case(prior["id"], prior["kind"], pack_ref, _load(path), pack_proc))

    phone_ref = _load(DEMO / "real_ref.graph.json")
    phone_proc = learn([phone_ref])
    phone = []
    for label in json.loads((DEMO / "real_labels.json").read_text(encoding="utf-8")):
        stem = Path(label["file"]).stem
        if stem in ("real_ref", "real_ok_1"):
            continue  # The reference and its own source recording are not attempts.
        phone.append(_case(stem, label["expected"], phone_ref, _load(DEMO / f"{stem}.graph.json"), phone_proc))

    data = {"type": "offline comparison of what each layer adds", "fresh_model_inference": False,
            "pipeline_sha256": pipeline_digest(),
            "final_picture_definition": "Accept when every required final placement holds in the attempt's last state. Order is ignored and it never abstains.",
            "limits": ["The final-picture check is our own simple comparison, written to show what order checking adds. It is not another product.",
                       "All three checks use the same saved video-model readings, so a reading error affects all of them.",
                       "These are development recordings that informed checker fixes, not a held-out benchmark.",
                       "The generated recordings come from one renderer. The phone recordings are one person, two blocks and one workspace, and contain no order-only mistake."],
            "generated": {"summary": _summary(generated), "cases": generated},
            "phone": {"summary": _summary(phone), "cases": phone}}
    (MEDIA / "baseline_comparison.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


if __name__ == "__main__":
    result = refresh()
    for name in ("generated", "phone"):
        print(name, json.dumps(result[name]["summary"], indent=1))
