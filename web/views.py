"""What the page shows: plain dictionaries built from the checker's report and procedure,
shared by the demo builder (`web.build_check`) and the live checker (`web.live`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from oneshot.check import Report, rule_view
from oneshot.procedure import Procedure
from oneshot.schema import TaskGraph
from oneshot.words import nice

KIND = {"skipped": "Skipped step", "order": "Wrong order", "wrong_place": "Wrong place",
        "extra": "Extra step", "unknown": "Couldn't verify", "doubt": "Conflicting evidence",
        "invalid_reference": "Reference needs review"}
WARN_KIND = {"order": "Different order"}


def report_view(report: Report, att: TaskGraph) -> dict:
    return {
        "passed": report.passed,
        "verdict": report.verdict,
        "verified": report.verified,
        "tracking_used": report.tracking_used,
        "evidence_status": report.evidence_status,
        "tracking": {"parser": att.meta.tracking_parser, "seconds": att.meta.track_seconds,
                     "objects_tracked": sum(bool(o.track) for o in att.objects),
                     "objects_total": len(att.objects), "errors": att.meta.tracking_errors},
        "milestones": report.milestones,
        "issues": [{"kind": (WARN_KIND.get(i.kind) if i.severity == "warn" else None) or KIND.get(i.kind, i.kind),
                    "type": i.kind, "severity": i.severity, "message": i.message, "time": i.time,
                    "critical": i.critical, "reason": i.reason, "quote": i.quote, "fix": i.fix}
                   for i in report.issues],
        "objects": [nice(o.id) for o in att.objects],
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
                    "-movflags", "+faststart", str(out)], check=True)
    return out
