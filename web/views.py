"""What the page shows: plain dictionaries built from the checker's report and procedure,
shared by the demo builder (`web.build_check`) and the live checker (`web.live`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from oneshot.check import Report, rule_view
from oneshot.procedure import Procedure
from oneshot.schema import TaskGraph
from oneshot.words import nice
from oneshot.media import MEDIA_TIMEOUT_SECONDS
from oneshot.tracking_identity import ambiguous_tracks

KIND = {"skipped": "Skipped step", "order": "Wrong order", "wrong_place": "Wrong place",
        "extra": "Extra step", "unknown": "Couldn't verify", "doubt": "Conflicting evidence",
        "invalid_reference": "Reference needs review"}
WARN_KIND = {"order": "Different order"}


def report_view(report: Report, att: TaskGraph) -> dict:
    ambiguous = ambiguous_tracks(att.objects)
    detections = []
    for obj in att.objects:
        error = att.meta.tracking_errors.get(obj.id)
        boxes = obj.track.boxes if obj.track else []
        state = ("ambiguous" if obj.id in ambiguous else "unavailable" if error else
                 "tracked" if boxes else "model_only" if att.meta.track_seconds is None else "unseen")
        detections.append({"id": obj.id, "name": nice(obj.id), "description": obj.concept(),
                           "kind": obj.kind, "status": state,
                           "first_seen": boxes[0].t_s if boxes else None,
                           "last_seen": boxes[-1].t_s if boxes else None})
    return {
        "passed": report.passed,
        "verdict": report.verdict,
        "verified": report.verified,
        "tracking_used": report.tracking_used,
        "evidence_status": report.evidence_status,
        "final_states": report.final_states,
        "order_checks": report.order_checks,
        "second_looks": report.second_looks,
        "tracking": {"parser": att.meta.tracking_parser, "seconds": att.meta.track_seconds,
                     "objects_tracked": sum(bool(o.track) for o in att.objects),
                     "objects_total": len(att.objects), "errors": att.meta.tracking_errors},
        "milestones": report.milestones,
        "issues": [{"kind": (WARN_KIND.get(i.kind) if i.severity == "warn" else None) or KIND.get(i.kind, i.kind),
                    "type": i.kind, "severity": i.severity, "message": i.message, "time": i.time,
                    "critical": i.critical, "reason": i.reason, "quote": i.quote, "fix": i.fix}
                   for i in report.issues],
        "objects": [nice(o.id) for o in att.objects],
        "detections": detections,
    }


def procedure_view(proc: Procedure) -> dict:
    turns = (proc.narration or {}).get("turns", [])
    return {
        "steps": [{"text": s.text, "start": s.start, "end": s.ref_time, "critical": s.critical,
                   "reason": s.reason, "quote": s.quote} for s in proc.steps],
        "rules": [rule_view(r, proc) for r in proc.shown()],
        "heard": [{"start": t["start"], "end": t["end"], "text": t["text"]} for t in turns],
        "references": proc.references,
    }


def add_audio(video: Path, audio_from: Path, out: Path) -> Path:
    """Copy `video` with the audio track of `audio_from` (if it has one)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio_from),
                    "-map", "0:v", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac", "-shortest",
                    "-movflags", "+faststart", str(out)], check=True, timeout=MEDIA_TIMEOUT_SECONDS)
    return out
