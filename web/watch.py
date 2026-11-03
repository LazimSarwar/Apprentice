"""Bounded camera sessions with adaptive reads and shared final verification."""
from __future__ import annotations

import math
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from oneshot.media import cut, video_info, small_mp4
from oneshot.procedure import Procedure
from oneshot.schema import TaskGraph
from oneshot.watch import Watch
from web import budget, live, storage
from web.views import procedure_view

WORK = storage.ROOT
MEDIA = live.WEB / "static" / "media"
DEMOS = ("pack_order", "pack_wrong_place", "pack_skip_liner", "pack_ok", "pack_skip_card", "pack_loose")
PER_SESSION = 4
MAX_READS = 60
MAX_ACTIVE = 2
IDLE = 30.0
MAX_CHUNK_BYTES = 2 * 1024 * 1024
READ_FPS = 5
READ_EFFORT = "minimal"
_pool = ThreadPoolExecutor(max_workers=2 * PER_SESSION)
_sessions: dict[str, "Session"] = {}
_lock = threading.RLock()


@dataclass
class Session:
    id: str
    mode: str
    ref: TaskGraph
    proc: Procedure
    src: Path
    duration: float | None = None
    video: str | None = None
    status: str = "watching"
    touched: float = field(default_factory=time.monotonic)
    started: float = field(default_factory=time.monotonic)
    watch: Watch = None
    seq: int = 0
    asked: float = 0.0
    pending: tuple | None = None
    in_flight: int = 0
    reads: int = 0
    failed: int = 0
    cost: float = 0.0
    read_seconds: list[float] = field(default_factory=list)
    received: int = 0
    final: dict | None = None
    error: str | None = None
    revision: int = 0
    seen: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    idle: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        self.watch = Watch(self.ref, self.proc)
        self.known = live._known(self.ref)
        self.idle.set()


def _reference(job, dataset="pack"):
    if job:
        saved = live.get(job)
        if not saved or not saved.get("procedure_confirmed"):
            raise live.UploadError("Confirm the learned procedure before using it with the camera.")
        ref, proc = live.load_reference(job)
        return ref, proc, True
    ref, proc = live.builtin(dataset)
    return ref, proc, False


def start(mode, demo=None, upload=None, reference_job=None, reference_demo="pack"):
    if mode not in ("camera", "replay"):
        raise live.UploadError("Choose camera or replay.")
    budget.ensure()
    cleanup()
    ref, proc, own = _reference(reference_job, reference_demo)
    sid = live.new_job_id()
    src = None
    seconds = None
    video = None
    if mode == "replay":
        if demo is not None:
            if demo not in DEMOS: raise KeyError(demo)
            src = MEDIA / f"{demo}_demo.mp4"
            video = f"media/{demo}_demo.mp4"
        elif upload is not None:
            src = upload
        else: raise live.UploadError("Select a sample or upload a replay.")
        seconds = live._check_length(src, "replay")
    with _lock:
        # Count and reserve in the same critical section.
        active = sum(s.status in ("watching", "finishing") for s in _sessions.values())
        if active >= MAX_ACTIVE:
            raise live.UploadError("Two live checks are already running. Try again in a minute.")
        folder = storage.create(sid)
        if upload is not None and src == upload:
            target = folder / "raw" / ("replay" + upload.suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(upload, target)
            src = target
        s = Session(sid, mode, ref, proc, src or folder / "camera.webm", seconds, video)
        _sessions[sid] = s
        storage.update(sid, event="session_started", reference_graph=ref.model_dump(), procedure=proc.to_dict())
        live._set(sid, status="watching", stage="Watching the live attempt", procedure_confirmed=True,
                  custom_reference=own, reference_demo=reference_demo, second_reference=False)
    return {"session": sid, "duration": seconds, "video": video, "own_reference": own,
        "steps": procedure_view(proc)["steps"], "max_seconds": live.MAX_SECONDS,
        "read_every_seconds": 2, "retention_seconds": storage.RETENTION_SECONDS}


def _get(sid):
    with _lock: s = _sessions.get(sid)
    if s is None: raise KeyError(sid)
    s.touched = time.monotonic()
    return s


def cancel(sid):
    s = _get(sid)
    with s.lock:
        if s.status in ("watching", "finishing"):
            s.status, s.pending = "cancelled", None
            s.revision += 1
            storage.update(s.id, event="session_cancelled")


def _camera_limit(s):
    if time.monotonic() - s.started > live.MAX_SECONDS:
        raise live.UploadError("The recording time limit was reached. Finish this check to verify the recording.")


def append(sid, data):
    s = _get(sid)
    with s.lock:
        if s.mode != "camera" or s.status != "watching":
            raise live.UploadError("This session is not accepting camera chunks.")
        _camera_limit(s)
        if len(data) > MAX_CHUNK_BYTES or s.received + len(data) > live.MAX_BYTES:
            raise live.UploadError("The recording or chunk exceeds its size limit.")
        with storage._lock:
            storage.ensure_space(len(data))
            with s.src.open("ab") as out: out.write(data)
        s.received += len(data)


def read(sid, upto=None):
    s = _get(sid)
    with s.lock:
        if s.status != "watching" or s.reads >= MAX_READS: return
        if s.mode == "camera":
            _camera_limit(s)
            if not s.src.exists(): return
            s.seq += 1
            s.pending = (s.seq, None)  # Snapshot only when a worker slot is admitted.
        else:
            if not isinstance(upto, (int, float)) or not math.isfinite(upto):
                raise live.UploadError("Replay time must be a finite number.")
            upto = min(float(upto), s.duration)
            if upto <= s.asked or upto < 1: return
            s.seq += 1; s.asked = upto; s.pending = (s.seq, upto)
    _launch(s)


def _launch(s):
    with s.lock:
        if s.pending is None or s.in_flight >= PER_SESSION or s.reads >= MAX_READS or s.status != "watching": return
        seq, upto = s.pending
        src = s.src
        if s.mode == "camera":
            src = live._work(s.id) / f"snapshot_{seq:03d}.webm"
            try:
                with storage._lock:
                    storage.ensure_space(s.src.stat().st_size)
                    shutil.copyfile(s.src, src)
            except (OSError, ValueError) as exc:
                src.unlink(missing_ok=True)
                s.status, s.pending, s.error = "error", None, str(exc)
                return
        s.pending = None; s.in_flight += 1; s.reads += 1
        s.idle.clear()
        _pool.submit(_read, s, seq, src, upto)


def _read(s, seq, src, upto):
    work = live._work(s.id) / f"read_{seq:03d}"
    try:
        begun = time.monotonic()
        clip = cut(src, upto, work / "prefix.mp4", fps=READ_FPS)
        seconds = video_info(clip).duration_s
        if seconds > live.MAX_SECONDS: raise live.UploadError("The recording exceeds its duration limit.")
        att = live._extract(clip, work, known_objects=s.known, effort=READ_EFFORT)
        with s.lock:
            if s.status != "watching": return
            s.cost += att.meta.extract_cost_usd or 0
            s.read_seconds.append(time.monotonic() - begun)
            s.watch.update(att, upto or seconds)
            s.revision += 1
    except budget.BudgetError as exc:
        with s.lock: s.status, s.pending, s.error = "error", None, str(exc)
    except Exception:
        traceback.print_exc()
        with s.lock: s.failed += 1
    finally:
        if s.mode == "camera": src.unlink(missing_ok=True)
        # Only remove this admitted read's dedicated folder.
        if work.exists() and work.resolve().parent == live._work(s.id).resolve():
            shutil.rmtree(work)
        with s.lock:
            s.in_flight -= 1
            if not s.in_flight: s.idle.set()
        _launch(s)


def finish(sid):
    s = _get(sid)
    with s.lock:
        if s.status != "watching": return
        s.status, s.pending = "finishing", None
    _pool.submit(_finish, s)


def _finish(s):
    try:
        if not s.idle.wait(timeout=180):
            raise live.UploadError("Earlier live reads did not finish in time. Review this recording or retry the check.")
        work = live._work(s.id)
        clip = cut(s.src, None, work / "whole.mp4")
        live._check_length(clip, "recording")
        att = live._extract(clip, work, known_objects=s.known)
        with s.lock:
            if s.status == "cancelled": return
        with live._lock: live._graphs[s.id] = (s.ref, att, s.proc)
        live._set(s.id, status="checked", stage="Checking with the tracker", procedure_confirmed=True)
        tracked, result, seen, note = live.finalize(s.id, s.ref, att, s.proc, small_mp4(clip, work / "clips"))
        video = s.video or live._web_video(clip, f"{s.id}_attempt.mp4")
        with s.lock:
            if s.status == "cancelled": return
            s.cost += att.meta.extract_cost_usd or 0
            s.cost += result.get("tracking", {}).get("accounted_usd", 0)
            s.final, s.video, s.seen, s.status = result, video, seen, "done"
            s.revision += 1
        live._set(s.id, status="done", stage=None, result=result, demo=video, seen=seen, note=note)
    except Exception as exc:
        traceback.print_exc()
        with s.lock: s.status, s.error = "error", f"Couldn't verify the recording ({type(exc).__name__}: {exc})."
        live._set(s.id, status="error", stage=None, error=s.error)


def get(sid):
    s = _get(sid)
    with s.lock:
        avg = sum(s.read_seconds) / len(s.read_seconds) if s.read_seconds else 0
        return {"session": sid, "mode": s.mode, "status": s.status, **s.watch.view(),
            "clock": round(time.monotonic()-s.started, 1), "reads": s.reads, "reading": s.in_flight,
            "failed": s.failed, "limit": MAX_READS, "read_seconds": round(avg, 1) if avg else None,
            "read_every_seconds": min(6, max(2, math.ceil(avg/PER_SESSION))) if avg else 2,
            "cost": round(s.cost, 4), "final": s.final, "video": s.video, "seen": s.seen,
            "error": s.error, "revision": s.revision}


def cleanup():
    now = time.monotonic()
    with _lock:
        for sid, s in list(_sessions.items()):
            with s.lock:
                if s.status == "watching" and (now-s.touched > IDLE or now-s.started > live.MAX_SECONDS+IDLE) and not s.in_flight:
                    s.status, s.pending = "cancelled", None
                try: storage.record(sid)
                except storage.AccessError:
                    if not s.in_flight: _sessions.pop(sid, None)
    live.cleanup()
