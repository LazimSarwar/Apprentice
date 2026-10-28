"""Near-live sessions: check an attempt while it is happening (`oneshot.watch`).

Two sources feed the same session:
  camera â€” the page records with the browser's camera and streams it here in one-second
           WebM chunks, appended to one file;
  replay â€” a finished video (a sample attempt, or an upload) played back in real time.

Every few seconds the page asks for a read: the video so far is cut into a small clip and
read by the video model, guided by the reference's object names. Reads overlap (up to
PER_SESSION at once) so a slow one doesn't hold up the next; a request that arrives while
all slots are busy replaces any older waiting one, because a longer read covers it. When the
attempt ends, the whole video gets the full check, which is the verdict.

Every read calls the hosted model (~$0.007), so sessions are capped in reads and length.
"""

from __future__ import annotations

import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from oneshot.graph import build_task_graph
from oneshot.media import cut, video_info
from oneshot.procedure import Procedure
from oneshot.schema import TaskGraph
from oneshot.watch import Watch
from web import live
from web.views import procedure_view, report_view

WORK = live.WORK.parent / "watch"
MEDIA = live.WEB / "static" / "media"
DEMOS = ("pack_order", "pack_wrong_place", "pack_skip_liner", "pack_ok", "pack_skip_card", "pack_loose")
PER_SESSION = 4            # reads of one attempt running at once (~8 s each, one every 2 s)
MAX_READS = 60             # ~2 minutes at one read every 2 s
MAX_ACTIVE = 2             # sessions watching at once (each read costs money)
IDLE = 30.0                # a session the page stopped asking about for this long doesn't count
READ_FPS = 5               # live reads: the model samples frames itself, so this only shrinks the upload
READ_EFFORT = "minimal"    # about halves a read; the final check on the whole video keeps "low"

_pool = ThreadPoolExecutor(max_workers=2 * PER_SESSION)
_sessions: dict[str, "Session"] = {}
_lock = threading.Lock()


@dataclass
class Session:
    id: str
    mode: str                          # camera | replay
    ref: TaskGraph
    proc: Procedure
    src: Path                          # replay: the whole video; camera: the growing recording
    duration: float | None = None      # replay only
    video: str | None = None           # what the page plays (replay sample / camera recording)
    status: str = "watching"           # watching | finishing | done | error | cancelled
    touched: float = field(default_factory=time.monotonic)    # last time the page asked anything
    started: float = field(default_factory=time.monotonic)
    watch: Watch = None
    seq: int = 0
    asked: float = 0.0                 # longest read requested so far (replay)
    pending: tuple | None = None
    in_flight: int = 0
    reads: int = 0
    failed: int = 0
    cost: float = 0.0
    read_seconds: list[float] = field(default_factory=list)
    received: int = 0
    final: dict | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.watch = Watch(self.ref, self.proc)
        self.known = [{"id": o.id, "color": o.color, "shape": o.shape} for o in self.ref.objects]


def _reference(job: str | None) -> tuple[TaskGraph, Procedure, bool]:
    if job:
        with live._lock:
            found = live._graphs.get(job)
        if found is None:
            raise KeyError(job)
        return found[0], found[2], True
    ref, proc = live.builtin()
    return ref, proc, False


def start(mode: str, demo: str | None = None, upload: Path | None = None,
          reference_job: str | None = None) -> dict:
    with _lock:
        active = sum(s.status in ("watching", "finishing") and time.monotonic() - s.touched < IDLE
                     for s in _sessions.values())
    if active >= MAX_ACTIVE:
        raise live.UploadError("Two live checks are already running â€” try again in a minute.")
    ref, proc, own = _reference(reference_job)
    sid = live.new_job_id()
    WORK.mkdir(parents=True, exist_ok=True)
    if mode == "camera":
        s = Session(sid, mode, ref, proc, WORK / f"{sid}_camera.webm")
    elif demo is not None:
        if demo not in DEMOS:
            raise KeyError(demo)
        src = MEDIA / f"{demo}_demo.mp4"
        s = Session(sid, "replay", ref, proc, src, video_info(src).duration_s, f"media/{demo}_demo.mp4")
    elif upload is not None:
        seconds = video_info(upload).duration_s
        if seconds > live.MAX_SECONDS:
            raise live.UploadError(f"The video is {seconds:.0f} s long; please keep it under {live.MAX_SECONDS} s.")
        s = Session(sid, "replay", ref, proc, upload, seconds)
    else:
        raise live.UploadError("Pick a sample attempt, upload a video, or use the camera.")
    with _lock:
        _sessions[sid] = s
    return {"session": sid, "duration": s.duration, "video": s.video, "own_reference": own,
            "steps": procedure_view(proc)["steps"]}


def _get(sid: str) -> Session:
    with _lock:
        s = _sessions.get(sid)
    if s is None:
        raise KeyError(sid)
    s.touched = time.monotonic()
    return s


def cancel(sid: str) -> None:
    """The page moved on: no more reads, no final check."""
    s = _get(sid)
    with s.lock:
        if s.status == "watching":
            s.status, s.pending = "cancelled", None


def append(sid: str, data: bytes) -> None:
    """One more chunk of the camera recording."""
    s = _get(sid)
    with s.lock:
        if s.mode != "camera" or s.status != "watching":
            return
        s.received += len(data)
        if s.received > live.MAX_BYTES:
            raise live.UploadError("The recording is too large.")
        with s.src.open("ab") as out:
            out.write(data)


def read(sid: str, upto: float | None = None) -> None:
    """Ask for a read of the attempt so far (replay: the first `upto` seconds)."""
    s = _get(sid)
    with s.lock:
        if s.status != "watching":
            return
        if s.mode == "camera":
            if not s.src.exists():
                return
            s.seq += 1
            snap = WORK / f"{sid}_snap{s.seq:03d}.webm"
            shutil.copyfile(s.src, snap)          # chunks are appended under the same lock
            s.pending = (s.seq, snap, None)
        else:
            upto = min(float(upto or 0.0), s.duration)
            if upto <= s.asked or upto < 1.0:
                return
            s.seq += 1
            s.asked = upto
            s.pending = (s.seq, s.src, upto)
    _launch(s)


def _launch(s: Session) -> None:
    with s.lock:
        if s.pending is None or s.in_flight >= PER_SESSION or s.reads >= MAX_READS or s.status != "watching":
            return
        job, s.pending = s.pending, None
        s.in_flight += 1
        s.reads += 1
    _pool.submit(_read, s, *job)


def _read(s: Session, seq: int, src: Path, upto: float | None) -> None:
    try:
        begun, t = time.monotonic(), time.perf_counter()
        clip = cut(src, upto, WORK / f"{s.id}_{seq:03d}.mp4", fps=READ_FPS)
        seconds = upto if upto is not None else video_info(clip).duration_s
        t1 = time.perf_counter()
        att = build_task_graph(clip, work_dir=WORK, known_objects=s.known, track=False, effort=READ_EFFORT)
        with s.lock:
            s.cost += att.meta.extract_cost_usd or 0.0
            s.read_seconds.append(round(time.perf_counter() - t, 1))
            used = s.watch.update(att, seconds)
        print(f"[watch {s.id}] read {seq} @{seconds:.1f}s: start +{begun - s.started:.1f}s"
              f" cut {t1 - t:.1f}s, graph {time.perf_counter() - t1:.1f}s (model {att.meta.extract_seconds}s), "
              f"done +{time.monotonic() - s.started:.1f}s, {'used' if used else 'stale'}, "
              f"{sum(a.status == 'open' for a in s.watch.alerts.values())} open", flush=True)
    except Exception:  # noqa: BLE001 - one failed read must not end the session
        traceback.print_exc()
        with s.lock:
            s.failed += 1
    finally:
        with s.lock:
            s.in_flight -= 1
        _launch(s)


def finish(sid: str) -> None:
    """The attempt is over: run the full check on the whole video."""
    s = _get(sid)
    with s.lock:
        if s.status != "watching":
            return
        s.status, s.pending = "finishing", None
    _pool.submit(_finish, s)


def _finish(s: Session) -> None:
    try:
        clip = cut(s.src, None, WORK / f"{s.id}_whole.mp4")
        att = build_task_graph(clip, work_dir=WORK, known_objects=s.known, track=False)
        report = s.watch.finish(att)
        video = s.video or live._web_video(clip, f"{s.id}_attempt.mp4")
        with live._lock:                          # lets the page re-check it with changed rules
            live._graphs[s.id] = (s.ref, att, s.proc)
        with s.lock:
            s.cost += att.meta.extract_cost_usd or 0.0
            s.final, s.video, s.status = report_view(report, att), video, "done"
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with s.lock:
            s.status, s.error = "error", f"Couldn't check the whole video ({type(exc).__name__}: {exc})."


def get(sid: str) -> dict:
    s = _get(sid)
    with s.lock:
        avg = sum(s.read_seconds) / len(s.read_seconds) if s.read_seconds else None
        return {"session": s.id, "mode": s.mode, "status": s.status, **s.watch.view(),
                "clock": round(time.monotonic() - s.started, 1),
                "reads": s.reads, "reading": s.in_flight, "failed": s.failed, "limit": MAX_READS,
                "read_seconds": round(avg, 1) if avg else None, "cost": round(s.cost, 4),
                "final": s.final, "video": s.video, "error": s.error}
