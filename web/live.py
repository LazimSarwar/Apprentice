"""Private video jobs, confirmed procedures, shared tracking and decision history."""
from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

import imageio.v3 as iio

from oneshot.check import compare
from oneshot.graph import add_evidence, build_task_graph
from oneshot.media import small_mp4, video_info
from oneshot.narration import listen, narrate, spoken_text, has_audio
from oneshot.procedure import Procedure, StepInfo, Rule, learn, milestones, final_keys, with_reference_objects, _closure
from oneshot.schema import TaskGraph
from oneshot.words import SURFACE
from web import budget, storage
from web.build_media import overlay_frames, write_mp4
from web.views import add_audio, procedure_view, report_view

WEB = Path(__file__).resolve().parent
REFERENCE = WEB / "reference.graph.json"
PROCEDURE = WEB / "reference.procedure.json"
DEMO = WEB / "demo"
WORK = storage.ROOT
UPLOADS = storage.ROOT  # Encoded user files are never under the static mount.
ALLOWED = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
MAX_BYTES = 80 * 1024 * 1024
MAX_SECONDS = int(os.environ.get("MAX_UPLOAD_SECONDS", "120"))
MAX_WAITING = 3
ACTIVE = {"queued", "running", "checked"}
_jobs: dict[str, dict] = {}
_graphs: dict[str, tuple[TaskGraph, TaskGraph, Procedure]] = {}
_pending: dict[str, tuple[Path | None, TaskGraph, Procedure]] = {}
_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=1)


class UploadError(ValueError):
    pass


def save_upload(fileobj: BinaryIO, filename: str, job_id: str, role: str) -> Path:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise UploadError("Unsupported video type. Use MP4, MOV, WebM, MKV, M4V or AVI.")
    path = storage.create(job_id) / "raw" / f"{role}{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with path.open("wb") as out:
            while chunk := fileobj.read(1 << 20):
                written += len(chunk)
                if written > MAX_BYTES:
                    raise UploadError("That video exceeds the 80 MB limit.")
                with storage._lock:
                    storage.ensure_space(len(chunk))
                    out.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if not written:
        path.unlink(missing_ok=True)
        raise UploadError("The video is empty.")
    return path


def new_job_id() -> str:
    return uuid.uuid4().hex


def ensure_capacity() -> None:
    budget.ensure()
    with _lock:
        if sum(j["status"] in ACTIVE for j in _jobs.values()) >= MAX_WAITING:
            raise UploadError("A few checks are already waiting. Try again in a minute.")


def submit(job_id: str, attempt: Path, reference: Path | None, reference2: Path | None = None,
           reference_demo: str = "pack") -> None:
    with _lock:
        ensure_capacity()
        storage.create(job_id)
        _jobs[job_id] = {"id": job_id, "status": "queued", "stage": "Waiting for another check to finish",
            "custom_reference": reference is not None, "second_reference": reference2 is not None,
            "reference_demo": reference_demo, "procedure_confirmed": reference is None}
        _set(job_id, **_jobs[job_id])
        _pool.submit(_run, job_id, attempt, reference, reference2, reference_demo)


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        if job: return dict(job)
    try:
        data = storage.record(job_id)
    except storage.AccessError:
        return None
    job = data.get("job")
    if job and job["status"] in ACTIVE | {"watching", "finishing"}:
        job = {**job, "status": "error", "stage": None,
               "error": "The server restarted during this check. The saved history is available; upload again."}
    return job


def _set(job_id: str, **fields) -> None:
    with _lock:
        try: saved = storage.record(job_id)
        except storage.AccessError: saved = {}
        previous = saved.get("job", {})
        _jobs.setdefault(job_id, previous or {"id": job_id}).update(fields)
        state = dict(_jobs[job_id])
        try:
            changed = any(state.get(k) != previous.get(k) for k in ("status", "stage", "result", "procedure_version"))
            storage.update(job_id, event="job_updated" if changed else None, job=state)
        except storage.AccessError:
            pass  # Isolated unit fixtures need not create private files.


def _work(job_id: str) -> Path:
    return storage.directory(job_id)


def _web_video(clip: Path, name: str, audio_from: Path | None = None) -> str:
    job_id = name.split("_", 1)[0]
    folder = _work(job_id) / "encoded"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    storage.ensure_space()
    write_mp4(target, iio.imiter(clip), float(iio.immeta(clip).get("fps", 30)))
    if audio_from is not None:
        tmp = target.with_suffix(".audio.mp4")
        add_audio(target, audio_from, tmp)
        shutil.move(tmp, target)
    return f"/api/assets/{job_id}/{name}"


def _check_length(path: Path, what: str) -> float:
    seconds = video_info(path).duration_s
    if seconds > MAX_SECONDS:
        raise UploadError(f"The {what} is {seconds:.0f} s long; keep it under {MAX_SECONDS} s.")
    return seconds


def builtin(dataset="pack") -> tuple[TaskGraph, Procedure]:
    if dataset not in ("pack", "real"):
        raise UploadError("Choose the phone or packing reference, or upload your own.")
    graph_path = DEMO / "real_ref.graph.json" if dataset == "real" else REFERENCE
    ref = TaskGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
    proc = learn([ref]) if dataset == "real" else Procedure.from_dict(json.loads(PROCEDURE.read_text(encoding="utf-8")))
    return ref, proc


def _extract(path, work, **kwargs):
    with budget.call("read video") as meter:
        result = build_task_graph(path, work_dir=work, track=False, **kwargs)
        # Also supports alternate extractor implementations that return usage metadata.
        if not meter.observed: meter.observe(result.meta.extract_cost_usd)
    return result


def _learn_own(job_id, reference, reference2):
    work = _work(job_id)
    seconds = _check_length(reference, "reference")
    _set(job_id, stage="Listening to your reference")
    turns = None
    if has_audio(reference):
        estimate = seconds / 3600 * budget.TRANSCRIBE_USD_PER_HOUR
        with budget.call("transcribe reference", estimate) as meter:
            turns = listen(reference, work / "audio")
            meter.observe(estimate, estimated=True)
    _set(job_id, stage="Reading the steps in your reference")
    ref = _extract(reference, work, spoken=spoken_text(turns))
    if not milestones(ref):
        raise UploadError("No checkable placements were learned. Record clear placements and try again.")
    refs = [ref]
    if reference2:
        _check_length(reference2, "second reference")
        _set(job_id, stage="Reading your second reference")
        refs.append(_extract(reference2, work, known_objects=_known(ref)))
    _set(job_id, stage="Learning which steps and orders matter")
    narration = None
    if turns:
        with budget.call("interpret reference narration") as meter:
            narration = narrate(turns, [(m.text, m.start, m.ref_time) for m in milestones(ref)])
            if not meter.observed: meter.observe(narration.cost_usd if narration else 0)
    proc = learn(refs, narration)
    # Expose inferred terminal requirements for human confirmation in the editor.
    proc.final_goals = sorted(final_keys(ref, {})) or None
    ref_clip = small_mp4(reference, work / "clips")
    _set(job_id, reference={"video": _web_video(ref_clip, f"{job_id}_reference.mp4", reference),
        **procedure_view(proc)}, editable_procedure=proc.to_dict(), reference_objects=[o.id for o in ref.objects])
    return ref, proc


def _known(ref):
    return [o.reference_hint() for o in ref.objects]


def _run(job_id, attempt, reference, reference2, reference_demo="pack"):
    try:
        budget.ensure()
        _set(job_id, status="running", stage="Opening the video")
        _check_length(attempt, "attempt")
        ref, proc = _learn_own(job_id, reference, reference2) if reference else builtin(reference_demo)
        if reference:
            with _lock: _pending[job_id] = (attempt, ref, proc)
            storage.update(job_id, event="procedure_proposed", reference_graph=ref.model_dump(),
                procedure=proc.to_dict(), pending_attempt=str(attempt.relative_to(_work(job_id))))
            _set(job_id, status="awaiting_confirmation", stage="Confirm the learned procedure", procedure_version=1)
            return
        _continue(job_id, attempt, ref, proc)
    except Exception as exc:
        _error(job_id, exc)


def submit_reference(job_id: str, reference: Path, title: str = "My procedure", reference2: Path | None = None):
    """Teach once without requiring an attempt or spending credits on it yet."""
    title = title.strip()
    if not title or len(title) > 100:
        raise UploadError("Give the procedure a name of at most 100 characters.")
    with _lock:
        ensure_capacity()
        storage.create(job_id)
        _jobs[job_id] = {"id": job_id, "title": title, "status": "queued", "stage": "Learning your reference",
            "custom_reference": True, "second_reference": reference2 is not None,
            "procedure_only": True, "procedure_confirmed": False}
        _set(job_id, **_jobs[job_id])
        _pool.submit(_run_reference, job_id, reference, reference2)


def _run_reference(job_id, reference, reference2):
    try:
        _set(job_id, status="running", stage="Reading your reference")
        ref, proc = _learn_own(job_id, reference, reference2)
        with _lock: _pending[job_id] = (None, ref, proc)
        storage.update(job_id, event="procedure_proposed", reference_graph=ref.model_dump(),
                       procedure=proc.to_dict(), pending_attempt=None)
        _set(job_id, status="awaiting_confirmation", stage="Confirm the learned procedure", procedure_version=1)
    except Exception as exc:
        _error(job_id, exc)


def _error(job_id, exc):
    traceback.print_exc()
    _set(job_id, status="error", stage=None, error=f"Couldn't complete this check ({type(exc).__name__}: {exc}).")


def _continue(job_id, attempt, ref, proc):
    try:
        _set(job_id, status="running", stage="Reading the steps in your attempt")
        att = _extract(attempt, _work(job_id), known_objects=_known(ref))
        clip = small_mp4(attempt, _work(job_id) / "clips")
        demo = _web_video(clip, f"{job_id}_attempt.mp4")
        report = compare(ref, att, procedure=proc)
        with _lock: _graphs[job_id] = (ref, att, proc)
        _set(job_id, status="checked", stage="Checking with the tracker", demo=demo, result=report_view(report, att))
        tracked, result, seen, note = finalize(job_id, ref, att, proc, clip)
        _set(job_id, status="done", stage=None, result=result, seen=seen, note=note)
    except Exception as exc:
        _error(job_id, exc)


def finalize(job_id, ref, att, proc, clip):
    """Both camera and upload completions use this evidence gate."""
    preliminary = report_view(compare(ref, att, procedure=proc), att)
    started = time.monotonic()
    note = None
    tracking_meter = None
    searched = with_reference_objects(att, ref, proc)
    try:
        # Up to two requests per object plus the bounded color fallback.
        with budget.call("track objects (estimated)", max(budget.TRACK_EST_USD, len(searched.objects) * 4 * budget.TRACK_EST_USD)) as tracking_meter:
            tracked = add_evidence(searched, clip)
    except Exception:
        traceback.print_exc()
        tracked = searched.model_copy(update={"meta": searched.meta.model_copy(update={
            "track_seconds": time.monotonic() - started,
            "tracking_errors": {o.id: "Tracking unavailable" for o in searched.objects},
            "warnings": [*att.meta.warnings, "Acceptance withheld: tracking unavailable."],
        })})
        note = "Tracking was unavailable. Review the recording or provide clearer evidence."
    with _lock:
        current = _graphs.get(job_id)
        if current: proc = current[2]  # Preserve edits made while tracking was running.
        _graphs[job_id] = (ref, tracked, proc)
    result = report_view(compare(ref, tracked, procedure=proc), tracked)
    result["tracking"]["accounted_usd"] = (tracking_meter.cost if tracking_meter.observed else tracking_meter.amount) if tracking_meter else 0
    _set(job_id, result=result, preliminary_result=preliminary)
    try:
        storage.update(job_id, event="evidence_assessed", reference_graph=ref.model_dump(),
            attempt_graph=tracked.model_dump(), procedure=proc.to_dict(), assessment=result)
    except storage.AccessError:
        pass
    seen = None
    if tracked.meta.tracking_errors != {o.id: "Tracking unavailable" for o in searched.objects}:
        try:
            target = _work(job_id) / "encoded" / f"{job_id}_attempt_seen.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            write_mp4(target, overlay_frames(clip, tracked), float(iio.immeta(clip).get("fps", 30)))
            seen = f"/api/assets/{job_id}/{target.name}"
        except Exception:
            traceback.print_exc()
            note = "The evidence assessment completed, but its video overlay could not be drawn."
    return tracked, result, seen, note


def confirm(job_id: str, data: dict) -> None:
    with _lock:
        if get(job_id)["status"] != "awaiting_confirmation":
            raise UploadError("This procedure is not waiting for confirmation.")
        pending = _pending.get(job_id)
        if pending is None:
            saved = storage.record(job_id)
            attempt_path = _work(job_id) / saved["pending_attempt"] if saved.get("pending_attempt") else None
            pending = (attempt_path, TaskGraph.model_validate(saved["reference_graph"]),
                       Procedure.from_dict(saved["procedure"]))
        attempt, ref, original = pending
        proc = Procedure.from_dict(data.get("procedure") or original.to_dict())
        validate_procedure(proc, ref)
        proc = proc.with_overrides(data.get("overrides") or {})
        validate_procedure(proc, ref)
        ensure_capacity()
        storage.update(job_id, event="procedure_confirmed", reference_graph=ref.model_dump(), procedure=proc.to_dict())
        _pending.pop(job_id, None)
        previous = get(job_id) or {}
        _set(job_id, procedure_confirmed=True, status="queued" if attempt else "ready",
             stage="Procedure confirmed; checking attempt" if attempt else None,
             reference={**(previous.get("reference") or {}), **procedure_view(proc)})
        if attempt: _pool.submit(_continue, job_id, attempt, ref, proc)


def validate_procedure(proc: Procedure, ref: TaskGraph):
    ids = {o.id for o in ref.objects} | {SURFACE}
    if any(k not in ids or v not in ("solid", "container", "surface") for k, v in proc.object_kinds.items()):
        raise UploadError("Object kinds must describe known objects as solid, container or surface.")
    keys = [s.key for s in proc.steps]
    if not keys or len(keys) > 128 or len(set(keys)) != len(keys):
        raise UploadError("Provide between one and 128 unique procedure steps.")
    for step in proc.steps:
        if (len(step.key) != 3 or step.key[0] not in ("on", "inside", "near")
                or any(x not in ids for x in step.key[1:]) or step.key[1] == step.key[2]
                or not isinstance(step.text, str) or not step.text.strip() or len(step.text) > 500
                or not math.isfinite(step.start) or not math.isfinite(step.ref_time)
                or not 0 <= step.start <= step.ref_time <= ref.video.duration_s):
            raise UploadError("Every step needs a valid relation, distinct known objects, and a short description.")
    for rule in proc.pairs:
        if rule.before not in keys or rule.after not in keys or rule.strength not in ("required", "free", "as_shown"):
            raise UploadError("Order rules must connect declared steps and have a valid strength.")
    if proc.final_goals is not None and (not proc.final_goals or any(k not in keys for k in proc.final_goals)):
        raise UploadError("Final goals must name one or more declared procedure steps.")
    if any(a == b for a, b in _closure({(r.before, r.after) for r in proc.pairs if r.strength == "required"})):
        raise UploadError("Required ordering rules cannot contain a cycle.")


def load_graphs(job_id):
    with _lock:
        found = _graphs.get(job_id)
    if found: return found
    data = storage.record(job_id)
    try:
        return TaskGraph.model_validate(data["reference_graph"]), TaskGraph.model_validate(data["attempt_graph"]), Procedure.from_dict(data["procedure"])
    except KeyError as exc: raise KeyError(job_id) from exc


def load_reference(job_id):
    state = get(job_id)
    if not state or not state.get("procedure_confirmed"):
        raise UploadError("Confirm the reference procedure first.")
    with _lock:
        cached = _graphs.get(job_id)
    if cached: return cached[0], cached[2]
    data = storage.record(job_id)
    return TaskGraph.model_validate(data["reference_graph"]), Procedure.from_dict(data["procedure"])


def edit_reference(job_id):
    """Reopen a saved reference; existing attempts/trials retain their own procedure copies."""
    with _lock:
        state = get(job_id)
        if not state or state.get("status") != "ready" or not state.get("procedure_only"):
            raise UploadError("Only a saved reference can be reopened for editing.")
        ref, proc = load_reference(job_id)
        _pending[job_id] = (None, ref, proc)
        storage.update(job_id, event="procedure_edit_started", pending_attempt=None)
        _set(job_id, status="awaiting_confirmation", procedure_confirmed=False,
             stage="Confirm the revised procedure", editable_procedure=proc.to_dict(),
             reference_objects=[o.id for o in ref.objects], procedure_version=state.get("procedure_version", 1)+1)


def submit_known(job_id, attempt, ref, proc, *, parent=None, metadata=None):
    """Schedule an attempt using a frozen, confirmed reference and procedure."""
    with _lock:
        ensure_capacity()
        storage.create(job_id)
        validate_procedure(proc, ref)
        _jobs[job_id] = {"id": job_id, "status": "queued", "stage": "Waiting to check your attempt",
            "custom_reference": True, "second_reference": False, "procedure_confirmed": True,
            "parent_job": parent, **(metadata or {})}
        storage.update(job_id, event="confirmed_procedure_reused", reference_graph=ref.model_dump(), procedure=proc.to_dict())
        _set(job_id, **_jobs[job_id])
        _pool.submit(_continue, job_id, attempt, ref, proc)


def list_procedures():
    out = []
    if not storage.ROOT.exists(): return out
    for path in storage.ROOT.iterdir():
        if not path.is_dir() or not storage._ID.fullmatch(path.name): continue
        try:
            storage.authorize(path.name)
            data = storage.record(path.name)
        except storage.AccessError: continue
        job = data.get("job") or {}
        if job.get("procedure_confirmed") and job.get("procedure_only"):
            out.append({"id": path.name, "title": job.get("title", "My procedure"), "created": data["created"],
                        "steps": len(data.get("procedure", {}).get("steps", [])), "status": job.get("status")})
    return sorted(out, key=lambda item: item["created"], reverse=True)


def list_checks():
    """Small, owner-scoped recovery index; never expose graphs or owner tokens."""
    out = []
    if not storage.ROOT.exists(): return out
    for path in storage.ROOT.iterdir():
        if not path.is_dir() or not storage._ID.fullmatch(path.name): continue
        try:
            storage.authorize(path.name)
            data = storage.record(path.name)
            state = get(path.name) or {}
        except storage.AccessError: continue
        if not state or state.get("procedure_only"): continue
        out.append({"id": path.name, "created": data["created"], "expires": data["expires"],
                    "status": state.get("status"), "verdict": (state.get("result") or {}).get("verdict"),
                    "human_decision": (state.get("human_review") or {}).get("decision")})
    return sorted(out, key=lambda item: item["created"], reverse=True)[:20]


def submit_followup(parent: str, job_id: str, attempt: Path):
    ref, proc = load_reference(parent)
    with _lock:
        ensure_capacity()
        previous = get(parent) or {}
        if previous.get("status") not in ("done", "ready"):
            raise UploadError("Complete the original assessment before adding a follow-up.")
        _jobs[job_id] = {"id": job_id, "status": "queued", "stage": "Checking your clearer recording",
            "custom_reference": previous.get("custom_reference", False), "second_reference": False,
            "reference_demo": previous.get("reference_demo", "pack"), "procedure_confirmed": True,
            "parent_job": parent, "reference": previous.get("reference")}
        storage.update(job_id, event="followup_created", parent_job=parent,
                       reference_graph=ref.model_dump(), procedure=proc.to_dict())
        storage.update(parent, event="followup_requested", followup_job=job_id)
        _set(job_id, **_jobs[job_id])
        _pool.submit(_continue, job_id, attempt, ref, proc)


def recheck(overrides, job=None, demo=None):
    if job is not None:
        ref, att, proc = load_graphs(job)
        proc = proc.with_overrides(overrides)
        validate_procedure(proc, ref)
        with _lock: _graphs[job] = (ref, att, proc)
        result = report_view(compare(ref, att, procedure=proc), att)
        _set(job, result=result)
        try: storage.update(job, event="rules_rechecked", procedure=proc.to_dict(), overrides=overrides, assessment=result)
        except storage.AccessError: pass
        return {job: result}
    ref, proc = builtin()
    proc = proc.with_overrides(overrides)
    out = {}
    for stem in demo or []:
        path = DEMO / f"{Path(stem).name}.graph.json"
        if not path.exists(): raise KeyError(stem)
        att = TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))
        out[stem] = report_view(compare(ref, att, procedure=proc), att)
    return out


def cleanup():
    removed = storage.cleanup()
    with _lock:
        for job in list(_jobs):
            try: storage.record(job)
            except storage.AccessError: removed.add(job)
        for job in removed:
            _jobs.pop(job, None); _graphs.pop(job, None); _pending.pop(job, None)
