"""Refresh judge-facing evidence from cached graphs, without model calls.

python -m web.build_judging            # reports + encode missing phone demo videos
python -m web.build_judging --reports-only

Raw personal recordings stay outside the repository. Only silent, web-encoded demo
copies and sanitized task graphs are included. Existing real cases are development
replays, never advertised as unseen evaluation data.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

import imageio.v3 as iio

from oneshot.check import compare
from oneshot.procedure import Procedure, learn
from oneshot.schema import TaskGraph
from oneshot.media import MEDIA_TIMEOUT_SECONDS
from web.build_media import overlay_frames, write_mp4
from web.views import procedure_view, report_view

APP = Path(__file__).resolve().parent.parent
RUNS = APP / "runs"
MEDIA = APP / "web/static/media"
DEMO = APP / "web/demo"
FOOTAGE = APP.parent / "Real-footage/procedure"
TITLES = {
    "real_ok_2": ("Correct · take 2", "The green block finishes on top of the red block."),
    "real_ok_3": ("Correct · take 3", "A separate correct attempt at a different pace."),
    "real_order_1": ("Stack reversed · take 1", "Red ends on green. This changes the structure, not just the order."),
    "real_order_2": ("Stack reversed · take 2", "Another attempt with the wrong block on top."),
    "real_wrongplace_1": ("Beside, not on · take 1", "Green is placed beside red instead of stacked on it."),
    "real_wrongplace_2": ("Beside, not on · take 2", "A second wrong-placement attempt."),
    "real_skip_1": ("Green missing · take 1", "Only the red block appears. Missing tracking evidence may require review."),
    "real_skip_2": ("Green missing · take 2", "A second missing-step attempt; abstention is shown explicitly."),
    "real_hidden_1": ("Hand blocks the view", "The hand partly covers the placement; inspect the evidence strength."),
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checker_digest() -> str:
    h = hashlib.sha256()
    for name in ("check", "procedure", "schema", "words", "semantics", "tracking_identity"):
        h.update((APP / f"oneshot/{name}.py").read_text(encoding="utf-8").replace('\r\n', '\n').encode())
    return h.hexdigest()


def pipeline_digest() -> str:
    sha = hashlib.sha256()
    for folder in ('oneshot', 'web'):
        for path in sorted((APP / folder).glob('*.py')):
            sha.update(path.name.encode())
            sha.update(path.read_text(encoding='utf-8').replace('\r\n', '\n').encode())
    for page in sorted((APP / 'web/static').glob('*.html')):
        sha.update(page.name.encode())
        sha.update(page.read_text(encoding='utf-8').replace('\r\n', '\n').encode())
    for name in ('web/pilot_protocol.json', 'requirements.lock'):
        sha.update((APP / name).read_text(encoding='utf-8').replace('\r\n', '\n').encode())
    return sha.hexdigest()


def load_real(stem: str) -> TaskGraph:
    cache = RUNS / f"{stem}.graph.json"
    exported = DEMO / f"{stem}.graph.json"
    path = cache if cache.exists() else exported
    graph = TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))
    graph = graph.model_copy(update={"source": f"{stem}.mp4"})
    exported.write_text(graph.model_dump_json(), encoding="utf-8")
    return graph


def encode(stem: str, graph: TaskGraph, reports_only: bool) -> dict:
    out = {"demo": f"media/{stem}_demo.mp4", "seen": f"media/{stem}_seen.mp4", "poster": f"media/{stem}.jpg"}
    if reports_only:
        return out
    clip = RUNS / "clips" / f"{stem}.720p.mp4"
    if not clip.exists():
        clip = FOOTAGE / f"{stem}.mp4"
    target = MEDIA / f"{stem}_demo.mp4"
    if not target.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-crf", "26", "-movflags", "+faststart", str(target)], check=True, timeout=MEDIA_TIMEOUT_SECONDS)
    overlay = MEDIA / f"{stem}_seen.mp4"
    if not overlay.exists():
        write_mp4(overlay, overlay_frames(clip, graph), float(iio.immeta(clip).get("fps", 30)))
    poster = MEDIA / f"{stem}.jpg"
    if not poster.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{graph.video.duration_s * 0.65:.3f}", "-i", str(target),
                        "-frames:v", "1", "-update", "1", str(poster)], check=True, timeout=MEDIA_TIMEOUT_SECONDS)
    out["poster"] = f"media/{stem}.jpg"
    return out


def refresh(reports_only: bool = False) -> None:
    MEDIA.mkdir(parents=True, exist_ok=True)
    DEMO.mkdir(parents=True, exist_ok=True)
    manifest = MEDIA / "check.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    ref = TaskGraph.model_validate_json((APP / "web/reference.graph.json").read_text(encoding="utf-8"))
    proc = Procedure.from_dict(json.loads((APP / "web/reference.procedure.json").read_text(encoding="utf-8")))
    for a in data["attempts"]:
        att = TaskGraph.model_validate_json((DEMO / f"{a['id']}.graph.json").read_text(encoding="utf-8"))
        a.update(report_view(compare(ref, att, procedure=proc), att))

    realref = load_real("real_ref")
    realproc = learn([realref])
    labels_path = FOOTAGE / "labels.csv"
    if labels_path.exists():
        labels = list(csv.DictReader(labels_path.read_text(encoding="utf-8").splitlines()))
        (DEMO / "real_labels.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")
    else:
        labels = json.loads((DEMO / "real_labels.json").read_text(encoding="utf-8"))
    attempts = []
    for label in labels:
        stem = Path(label["file"]).stem
        if stem in ("real_ref", "real_ok_1"):
            continue
        att = load_real(stem)
        report = compare(realref, att, procedure=realproc)
        title, what = TITLES[stem]
        expected = label["expected"]
        expected_fail = expected not in ("pass", "no_false_fail")
        resolved = report.verdict != "unsure"
        matches = resolved and (report.verdict == "fail" if expected_fail else report.passed)
        media = encode(stem, att, reports_only)
        attempts.append({"id": stem, "title": title, "what": what, "original_label": expected,
                         "expected_outcome": "fail" if expected_fail else "no false failure" if expected == "no_false_fail" else "pass",
                         "resolved_correctly": matches,
                         "clip_sha256": digest(MEDIA / f"{stem}_demo.mp4") if (MEDIA / f"{stem}_demo.mp4").exists() else None,
                         **media, **report_view(report, att)})
    self_graph = load_real("real_ok_1")
    real = {
        "reference": {"id": "real_ref", **encode("real_ref", realref, reports_only), **procedure_view(realproc)},
        "attempts": attempts,
        "self_check": {"excluded_from_score": True, "reason": "Same source recording as the reference.",
                       **report_view(compare(realref, self_graph, procedure=realproc), self_graph)},
        "summary": {
            "independent_attempts": len(attempts),
            "resolved_correctly": sum(a["resolved_correctly"] for a in attempts),
            "abstentions": sum(a["verdict"] == "unsure" for a in attempts),
            "false_accepts": sum(a["expected_outcome"] == "fail" and a["passed"] for a in attempts),
            "false_rejects": sum(a["expected_outcome"] != "fail" and a["verdict"] == "fail" for a in attempts),
            "incorrect_attempts": sum(a["expected_outcome"] == "fail" for a in attempts),
        },
        "method": {"type": "development replay", "fresh_model_inference": False,
                   "checker_sha256": checker_digest(),
                   "scope": "Two blocks, one workspace, one person. These clips informed earlier checker fixes; they are not a held-out benchmark.",
                   "order_caveat": "The two reversed stacks change the final structure and are diagnosed as wrong placement. They do not test pure order-only errors.",
                   "tracking_caveat": "2D boxes support object visibility and approximate placement; they do not establish 3D contact. Partial evidence is labelled.",
                   "reference_caveat": "The red block is named pink by the extractor. The same learned identifier is reused across attempts."},
    }
    data["real"] = real
    fresh = MEDIA / "judging_eval.json"
    if fresh.exists():
        evaluation = json.loads(fresh.read_text(encoding="utf-8"))
        data["fresh_evaluation"] = evaluation
        data["fresh_evaluation_current"] = evaluation["plan"]["checker_sha256"] == checker_digest()
    repair = MEDIA / 'tracking_validation.json'
    data.pop('tracking_validation', None)
    if repair.exists():
        validation = json.loads(repair.read_text(encoding='utf-8'))
        if validation['pipeline_sha256'] == pipeline_digest():
            data['tracking_validation'] = validation
    live_proof = MEDIA / 'live_validation.json'
    data.pop('live_validation', None)
    if live_proof.exists():
        validation = json.loads(live_proof.read_text(encoding='utf-8'))
        if validation['pipeline_sha256'] == pipeline_digest():
            data['live_validation'] = validation
    from web.replay_validation import refresh as replay
    data['current_validation'] = replay()
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    (MEDIA / "real_results.json").write_text(json.dumps(real, indent=2), encoding="utf-8")
    print("Real footage:", json.dumps(real["summary"]))
    for a in attempts:
        print(a["id"], a["verdict"], a["evidence_status"])
    print("Reference self-check (excluded):", real["self_check"]["verdict"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-only", action="store_true")
    refresh(parser.parse_args().reports_only)
