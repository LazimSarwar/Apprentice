"""Live checking: run the real pipeline on uploaded videos, one job at a time.

A job learns the procedure — the built-in demo one, or the user's own: their reference is
listened to (what they said names the objects and gives reasons and order rules), read, and
optionally refined by a second correct reference. It then reads the attempt (guided by the
reference's object names), compares, and publishes the verdict as soon as it exists; finally
it tracks the objects, re-checks the verdict against the tracker's evidence and publishes the
AI-vision video. Graphs stay in memory so a person can change a rule and re-check instantly.

Every check calls the hosted models, so uploads are size- and length-limited and jobs run
one at a time.
"""

from __future__ import annotations

import json
import os
import shutil
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
from oneshot.narration import listen, narrate, spoken_text
from oneshot.procedure import Procedure, learn, milestones
from oneshot.schema import TaskGraph
from web import budget
from web.build_media import overlay_frames, write_mp4
from web.views import add_audio, procedure_view, report_view

WEB = Path(__file__).resolve().parent
REFERENCE = WEB / "reference.graph.json"
PROCEDURE = WEB / "reference.procedure.json"
DEMO = WEB / "demo"                             # tracked demo attempt graphs, for re-checking
UPLOADS = WEB / "static" / "uploads"            # web-encoded videos the page plays
WORK = WEB.parent / "runs" / "uploads"          # raw uploads and intermediate clips
ALLOWED = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
MAX_BYTES = 80 * 1024 * 1024
MAX_SECONDS = int(os.environ.get("MAX_UPLOAD_SECONDS", "120"))
MAX_WAITING = 3             # jobs queued or running; beyond that, ask people to come back

_jobs: dict[str, dict] = {}
_graphs: dict[str, tuple[TaskGraph, TaskGraph, Procedure]] = {}    # job -> (reference, attempt, procedure)
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


def ensure_capacity() -> None:
    """Before taking an upload: the spend cap, and a short queue (checks run one at a time)."""
    budget.ensure()
    with _lock:
        waiting = sum(j["status"] in ("queued", "running") for j in _jobs.values())
    if waiting >= MAX_WAITING:
        raise UploadError("A few checks are already waiting — try again in a minute.")


def submit(job_id: str, attempt: Path, reference: Path | None, reference2: Path | None = None) -> None:
    with _lock:
        _jobs[job_id] = {"id": job_id, "status": "queued", "stage": "Waiting for another check to finish",
                         "custom_reference": reference is not None, "second_reference": reference2 is not None}
    _pool.submit(_run, job_id, attempt, reference, reference2)


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _set(job_id: str, **fields) -> None:
    with _lock:
        _jobs[job_id].update(fields)


def _web_video(clip: Path, name: str, audio_from: Path | None = None) -> str:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    fps = float(iio.immeta(clip).get("fps", 30))
    target = UPLOADS / name
    write_mp4(target, iio.imiter(clip), fps)
    if audio_from is not None:
        tmp = target.with_suffix(".audio.mp4")
        add_audio(target, audio_from, tmp)
        shutil.move(tmp, target)
    return f"uploads/{name}"


def _check_length(path: Path, what: str) -> float:
    seconds = video_info(path).duration_s
    if seconds > MAX_SECONDS:
        raise UploadError(f"The {what} is {seconds:.0f} s long; please keep it under {MAX_SECONDS} s.")
    return seconds


def builtin() -> tuple[TaskGraph, Procedure]:
    ref = TaskGraph.model_validate_json(REFERENCE.read_text(encoding="utf-8"))
    proc = Procedure.from_dict(json.loads(PROCEDURE.read_text(encoding="utf-8")))
    return ref, proc


def _learn_own(job_id: str, reference: Path, reference2: Path | None) -> tuple[TaskGraph, Procedure]:
    seconds = _check_length(reference, "reference")
    _set(job_id, stage="Listening to your reference")
    turns = listen(reference, WORK / "audio")
    budget.charge(seconds / 3600 * budget.TRANSCRIBE_USD_PER_HOUR, "transcribe reference")
    _set(job_id, stage="Reading the steps in your reference")
    ref = build_task_graph(reference, work_dir=WORK, track=False, spoken=spoken_text(turns))
    budget.charge(ref.meta.extract_cost_usd, "read reference")
    refs = [ref]
    if reference2 is not None:
        _check_length(reference2, "second reference")
        _set(job_id, stage="Reading your second reference")
        known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in ref.objects]
        refs.append(build_task_graph(reference2, work_dir=WORK, known_objects=known, track=False))
        budget.charge(refs[-1].meta.extract_cost_usd, "read second reference")
    _set(job_id, stage="Learning which steps and orders matter")
    narration = narrate(turns, [(m.text, m.start, m.ref_time) for m in milestones(ref)])
    proc = learn(refs, narration)
    ref_clip = WORK / "clips" / f"{reference.stem}.720p.mp4"
    _set(job_id, reference={"video": _web_video(ref_clip, f"{job_id}_reference.mp4", audio_from=reference),
                            **procedure_view(proc)})
    return ref, proc


def _run(job_id: str, attempt: Path, reference: Path | None, reference2: Path | None) -> None:
    try:
        _set(job_id, status="running", stage="Opening the video")
        _check_length(attempt, "attempt")
        ref, proc = _learn_own(job_id, reference, reference2) if reference is not None else builtin()

        _set(job_id, stage="Reading the steps in your attempt")
        known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in ref.objects]
        att = build_task_graph(attempt, work_dir=WORK, known_objects=known, track=False)
        budget.charge(att.meta.extract_cost_usd, "read attempt")
        att_clip = WORK / "clips" / f"{attempt.stem}.720p.mp4"
        demo = _web_video(att_clip, f"{job_id}_attempt.mp4")

        _set(job_id, stage="Comparing with the procedure")
        report = compare(ref, att, procedure=proc)
        with _lock:
            _graphs[job_id] = (ref, att, proc)
        _set(job_id, status="checked", stage="Checking with the tracker", demo=demo,
             result=report_view(report, att))
    except UploadError as exc:
        _set(job_id, status="error", stage=None, error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - report any pipeline failure to the page
        traceback.print_exc()
        _set(job_id, status="error", stage=None, error=f"Couldn't check this video ({type(exc).__name__}: {exc}).")
        return

    try:                                   # evidence is a bonus: a failure here keeps the verdict
        tracked = add_evidence(att, att_clip)
        budget.charge(budget.TRACK_EST_USD, "track attempt (estimate)")
        with _lock:
            _graphs[job_id] = (ref, tracked, proc)
        report = compare(ref, tracked, procedure=proc)
        fps = float(iio.immeta(att_clip).get("fps", 30))
        write_mp4(UPLOADS / f"{job_id}_attempt_seen.mp4", overlay_frames(att_clip, tracked), fps)
        _set(job_id, status="done", stage=None, seen=f"uploads/{job_id}_attempt_seen.mp4",
             result=report_view(report, tracked))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        _set(job_id, status="done", stage=None, seen=None, note="The tracking view couldn't be drawn for this video.")


def recheck(overrides: dict[str, str], job: str | None = None, demo: list[str] | None = None) -> dict:
    """Re-judge attempts with a person's rule choices — no model calls, instant."""
    out: dict[str, dict] = {}
    if job is not None:
        with _lock:
            found = _graphs.get(job)
        if found is None:
            raise KeyError(job)
        ref, att, proc = found
        out[job] = report_view(compare(ref, att, procedure=proc.with_overrides(overrides)), att)
        return out
    ref, proc = builtin()
    proc = proc.with_overrides(overrides)
    for stem in demo or []:
        path = DEMO / f"{Path(stem).name}.graph.json"
        if not path.exists():
            raise KeyError(stem)
        att = TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))
        out[stem] = report_view(compare(ref, att, procedure=proc), att)
    return out
