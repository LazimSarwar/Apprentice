"""Frozen prospective trials; labels never enter model extraction prompts."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import statistics
import threading
import time
from pathlib import Path

from oneshot.media import source_digest
from oneshot.procedure import Procedure, _closure
from oneshot.schema import TaskGraph
from web import live, storage

PROTOCOL = Path(__file__).with_name("pilot_protocol.json")
_lock = threading.RLock()


def protocol():
    return json.loads(PROTOCOL.read_text(encoding="utf-8"))


def _digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create(reference_job: str, fingerprint: str, reference_participant="A", roles=None):
    storage.authorize(reference_job)
    ref, proc = live.load_reference(reference_job)
    live.validate_procedure(proc, ref)
    roles = roles or {}
    names = ("liner", "product", "accessory", "card")
    if (not isinstance(roles, dict) or set(roles) != set(names) or len(proc.steps) != 4
            or any(type(roles[n]) is not int or not 0 <= roles[n] < 4 for n in names)
            or len(set(roles.values())) != 4):
        raise live.UploadError("This protocol needs exactly four packing placements. Identify the liner, product, accessory and card steps before freezing.")
    liner, product, accessory, card = (proc.steps[roles[n]].key for n in names)
    if not (liner[0] == accessory[0] == "inside" and liner[2] == accessory[2]
            and product == ("on", product[1], liner[1])
            and card == ("on", card[1], product[1])
            and len({liner[1], product[1], accessory[1], card[1], liner[2]}) == 5):
        raise live.UploadError("Confirm liner inside box, product on liner, accessory inside the same box, and card on product.")
    required = _closure({(r.before, r.after) for r in proc.pairs if r.strength == "required"})
    if any((liner, key) not in required for key in (product, accessory, card)):
        raise live.UploadError("The protocol requires the liner before the product, accessory and card. Confirm these rules in the procedure editor.")
    if proc.final_goals is None or set(proc.final_goals) != {s.key for s in proc.steps}:
        raise live.UploadError("Keep all four placements as required final goals for this trial.")
    if not isinstance(reference_participant, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", reference_participant):
        raise live.UploadError("Use an anonymous reference participant code, such as A.")
    plan = {"protocol": protocol(), "pipeline_sha256": fingerprint,
            "procedure": proc.to_dict(), "reference_graph": ref.model_dump(),
            "reference_participant": reference_participant, "roles": roles, "created": time.time(),
            "case_labels": {c["id"]: c["expected"] for c in protocol()["cases"]}}
    sid = live.new_job_id()
    storage.create(sid)
    storage.update(sid, event="trial_frozen", kind="pilot", plan=plan, plan_sha256=_digest(plan),
                   reference_job=reference_job, submissions={})
    return {"pilot": sid, "plan_sha256": _digest(plan), "cases": plan["protocol"]["cases"]}


def _load(sid):
    storage.authorize(sid)
    data = storage.record(sid)
    if data.get("kind") != "pilot" or _digest(data["plan"]) != data.get("plan_sha256"):
        raise live.UploadError("The frozen trial plan is missing or changed.")
    return data


def submit(sid, case_id, participant, fileobj, filename, fingerprint, *, label_confirmed=False, retakes=0):
    data = _load(sid)
    if fingerprint != data["plan"]["pipeline_sha256"]:
        raise live.UploadError("The checker changed since this trial was frozen. Start a new trial; keep this one as development evidence.")
    if case_id not in data["plan"]["case_labels"]:
        raise live.UploadError("Choose a case from the frozen protocol.")
    if not label_confirmed:
        raise live.UploadError("Confirm that a human checked this recording against its predeclared label before inference.")
    if not isinstance(participant, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", participant):
        raise live.UploadError("Use an anonymous participant code, such as B.")
    if not isinstance(retakes, int) or not 0 <= retakes <= 20:
        raise live.UploadError("Record the number of retakes, between zero and twenty.")
    job_id = live.new_job_id()
    live.ensure_capacity()
    with _lock:
        current = _load(sid)
        if case_id in current["submissions"]:
            raise live.UploadError("This case already has a submission. Preserve its first result and start a new trial for retesting.")
        row = {"job": job_id, "participant": participant, "retakes": retakes,
               "human_label_confirmed": True, "submitted": time.time(), "status": "uploading"}
        submissions = {**current["submissions"], case_id: row}
        storage.update(sid, event="trial_case_reserved", submissions=submissions)
    enqueued = False
    try:
        attempt = live.save_upload(fileobj, filename, job_id, "attempt")
        digest = source_digest(attempt)
        with _lock:
            current = _load(sid)
            if any(r.get("clip_sha256") == digest for r in current["submissions"].values()):
                raise live.UploadError("This recording was already used in this trial. Submit a separate take for each case.")
            current["submissions"][case_id]["clip_sha256"] = digest
            storage.update(sid, event="trial_clip_identified", submissions=current["submissions"])
        ref = TaskGraph.model_validate(data["plan"]["reference_graph"])
        proc = Procedure.from_dict(data["plan"]["procedure"])
        # Only the recording, reference object descriptions and confirmed procedure
        # reach the pipeline. Neither expected labels nor participant codes are prompts.
        live.submit_known(job_id, attempt, ref, proc, parent=data["reference_job"],
                          metadata={"pilot": sid, "pilot_case": case_id})
        enqueued = True
        with _lock:
            current = _load(sid)
            current["submissions"][case_id].update(status="submitted", clip_sha256=digest)
            storage.update(sid, event="trial_case_submitted", submissions=current["submissions"])
        return {"job": job_id, "case": case_id, "clip_sha256": digest}
    except Exception as exc:
        if not enqueued: storage.remove(job_id)
        with _lock:
            current = _load(sid)
            current["submissions"][case_id].update(status="submitted" if enqueued else "submission_error", error=type(exc).__name__)
            storage.update(sid, event="trial_submission_failed", submissions=current["submissions"])
        raise


def timing(sid, case_id, manual_seconds, assisted_seconds):
    data = _load(sid)
    if case_id not in data["submissions"]:
        raise live.UploadError("Submit this case before adding review timing.")
    for value in (manual_seconds, assisted_seconds):
        if value is not None and (not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 7200):
            raise live.UploadError("Review times must be positive seconds, up to two hours.")
    with _lock:
        data = _load(sid)
        data["submissions"][case_id]["review_timing"] = {
            "manual_seconds": manual_seconds, "assisted_seconds": assisted_seconds,
            "source": "self-timed human review; excludes inference latency", "recorded": time.time()}
        storage.update(sid, event="review_timing_recorded", submissions=data["submissions"])


def report(sid, fingerprint):
    data = _load(sid)
    rows = []
    participants = set()
    latencies, differences = [], []
    for case in data["plan"]["protocol"]["cases"]:
        row = {**case, "submitted": False}
        submitted = data["submissions"].get(case["id"])
        if submitted:
            row.update(submitted, submitted=True)
            participants.add(submitted["participant"])
            job = live.get(submitted["job"])
            row["status"] = job["status"] if job else submitted["status"]
            result = (job or {}).get("result") if row["status"] == "done" else None
            row["result"] = result
            if result:
                expected = case["expected"]
                fails = {i["type"] for i in result["issues"] if i["severity"] == "fail"}
                row["matched_expected"] = (result["passed"] if expected == "pass" else
                    result["verdict"] == "unsure" if expected == "review" else
                    result["verdict"] == "fail" and expected in fails)
                row["false_accept"] = expected in ("order", "skipped", "wrong_place") and result["passed"]
                row["false_reject"] = expected == "pass" and result["verdict"] == "fail"
                row["review_case_accepted"] = expected == "review" and result["passed"]
                row["diagnostically_resolved"] = expected != "review" and row["matched_expected"]
                try:
                    history = storage.record(submitted["job"])["history"]
                    ended = next(h["at"] for h in reversed(history) if (h.get("job") or {}).get("status") == "done")
                    row["model_wall_seconds"] = round(ended-submitted["submitted"], 2)
                    latencies.append(row["model_wall_seconds"])
                except (storage.AccessError, StopIteration): pass
            t = submitted.get("review_timing", {})
            if t.get("manual_seconds") and t.get("assisted_seconds"):
                differences.append(t["manual_seconds"]-t["assisted_seconds"])
        rows.append(row)
    complete = [r for r in rows if r.get("result")]
    return {"type": "prospective packing trial", "pilot": sid, "plan_sha256": data["plan_sha256"],
        "pipeline_sha256": data["plan"]["pipeline_sha256"], "source_matches": fingerprint == data["plan"]["pipeline_sha256"],
        "created": data["plan"]["created"], "expires": data["expires"], "reference_job": data["reference_job"],
        "plan": data["plan"], "results": rows,
        "summary": {"planned": len(rows), "submitted": sum(r["submitted"] for r in rows), "completed": len(complete),
            "matched_expected": sum(r.get("matched_expected", False) for r in complete),
            "diagnostically_resolved": sum(r.get("diagnostically_resolved", False) for r in complete),
            "reviews": sum(r["result"]["verdict"] == "unsure" for r in complete),
            "false_accepts": sum(r.get("false_accept", False) for r in complete),
            "false_rejects": sum(r.get("false_reject", False) for r in complete),
            "review_cases_accepted": sum(r.get("review_case_accepted", False) for r in complete),
            "pipeline_errors": sum(r.get("status") in ("error", "submission_error") for r in rows),
            "participant_codes_self_reported": sorted(participants),
            "second_person_attempts_self_reported": sum(r["submitted"] and r.get("participant") != data["plan"]["reference_participant"] for r in rows),
            "median_model_wall_seconds": statistics.median(latencies) if latencies else None,
            "paired_review_timings": len(differences),
            "median_self_timed_review_difference_seconds": statistics.median(differences) if differences else None},
        "limits": ["Participant identities and review timings are self-reported.",
            "Self-timed review differences exclude model waiting time and are not a controlled impact study.",
            "Review cases stay separate from diagnostic coverage; pipeline errors remain in the planned denominator.",
            "The original submission is retained. Corrections and retakes need a separate trial.",
            "2D tracking cannot establish physical contact or workplace safety."]}
