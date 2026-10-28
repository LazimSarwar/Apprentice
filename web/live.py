"""Live checking: run the real pipeline on uploaded videos, one job at a time.

A job reads the attempt (guided by the reference's object names), compares it with the
reference, and publishes the verdict as soon as it exists; it then tracks the objects and
publishes the AI-vision video. The reference is the built-in demo procedure unless the
user uploads their own, in which case it is read first.

Every check calls the hosted models, so uploads are size- and length-limited and jobs run
one at a time.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

import imageio.v3 as iio

from oneshot.check import compare
from oneshot.graph import add_evidence, build_task_graph
from oneshot.media import video_info
from oneshot.schema import TaskGraph
from oneshot.words import nice, step_text
from web.build_media import overlay_frames, write_mp4

WEB = Path(__file__).resolve().parent
REFERENCE = WEB / "reference.graph.json"
UPLOADS = WEB / "static" / "uploads"            # web-encoded videos the page plays
WORK = WEB.parent / "runs" / "uploads"          # raw uploads and intermediate clips
ALLOWED = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
MAX_BYTES = 80 * 1024 * 1024
MAX_SECONDS = 120
KIND = {"skipped": "Skipped step", "order": "Wrong order", "wrong_place": "Wrong place", "extra": "Extra step"}

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=1)


class UploadError(ValueError):
    pass


def save_upload(fileobj: BinaryIO, filename: str, job_id: str, role: str) -> Path:
    """Stream an upload to disk, enforcing the type and size limits."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise UploadError(f"Unsupported file type '{ext or '?'}'. Use one of: {', '.join(sorted(ALLOWED))}.")
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / f"{job_id}_{role}{ext}"
    written = 0
    with path.open("wb") as out:
        while chunk := fileobj.read(1 << 20):
            written += len(chunk)
            if written > MAX_BYTES:
                out.close()
                path.unlink(missing_ok=True)
                raise UploadError(f"That file is over {MAX_BYTES // (1024 * 1024)} MB.")
            out.write(chunk)
    return path


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def submit(job_id: str, attempt: Path, reference: Path | None) -> None:
    with _lock:
        _jobs[job_id] = {"id": job_id, "status": "queued", "stage": "Waiting for another check to finish",
                         "custom_reference": reference is not None}
    _pool.submit(_run, job_id, attempt, reference)


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _set(job_id: str, **fields) -> None:
    with _lock:
        _jobs[job_id].update(fields)


def _web_video(clip: Path, name: str) -> str:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    fps = float(iio.immeta(clip).get("fps", 30))
    write_mp4(UPLOADS / name, iio.imiter(clip), fps)
    return f"uploads/{name}"


def _check_length(path: Path, what: str) -> None:
    seconds = video_info(path).duration_s
    if seconds > MAX_SECONDS:
        raise UploadError(f"The {what} is {seconds:.0f} s long; please keep it under {MAX_SECONDS} s.")


def _run(job_id: str, attempt: Path, reference: Path | None) -> None:
    try:
        _set(job_id, status="running", stage="Opening the video")
        _check_length(attempt, "attempt")
        if reference is not None:
            _check_length(reference, "reference")
            _set(job_id, stage="Reading the steps in your reference")
            ref = build_task_graph(reference, work_dir=WORK, track=False)
            ref_clip = WORK / "clips" / f"{reference.stem}.720p.mp4"
            _set(job_id, reference={
                "video": _web_video(ref_clip, f"{job_id}_reference.mp4"),
                "steps": [step_text(s) for s in sorted(ref.steps, key=lambda s: s.order)],
            })
        else:
            ref = TaskGraph.model_validate_json(REFERENCE.read_text(encoding="utf-8"))

        _set(job_id, stage="Reading the steps in your attempt")
        known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in ref.objects]
        att = build_task_graph(attempt, work_dir=WORK, known_objects=known, track=False)
        att_clip = WORK / "clips" / f"{attempt.stem}.720p.mp4"
        demo = _web_video(att_clip, f"{job_id}_attempt.mp4")

        _set(job_id, stage="Comparing with the reference")
        report = compare(ref, att)
        _set(job_id, status="checked", stage="Drawing what the AI saw", demo=demo, result={
            "passed": report.passed,
            "milestones": report.milestones,
            "issues": [{"kind": KIND.get(i.kind, i.kind), "severity": i.severity,
                        "message": i.message, "time": i.time} for i in report.issues],
            "objects": [nice(o.id) for o in att.objects],
        })
    except UploadError as exc:
        _set(job_id, status="error", stage=None, error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - report any pipeline failure to the page
        traceback.print_exc()
        _set(job_id, status="error", stage=None, error=f"Couldn't check this video ({type(exc).__name__}: {exc}).")
        return

    try:                                   # evidence is a bonus: a failure here keeps the verdict
        tracked = add_evidence(att, att_clip)
        fps = float(iio.immeta(att_clip).get("fps", 30))
        write_mp4(UPLOADS / f"{job_id}_attempt_seen.mp4", overlay_frames(att_clip, tracked), fps)
        _set(job_id, status="done", stage=None, seen=f"uploads/{job_id}_attempt_seen.mp4")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        _set(job_id, status="done", stage=None, seen=None, note="The tracking view couldn't be drawn for this video.")
