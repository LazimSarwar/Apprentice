"""OneShot web app server.

Serves the demo pages (built by `web.build_check` / `web.build_media`) and a live checker:
upload an attempt video — and optionally your own reference (plus a second correct one) — and
get the verdict. Rule changes on the page are re-checked instantly via /api/recheck.
/api/watch checks an attempt while it happens (camera, or a video replayed in real time).

Run from app/:  python -m uvicorn web.server:app --port 8000
"""

from __future__ import annotations

from pathlib import Path
import asyncio
import json
import secrets
import re
from contextlib import asynccontextmanager, suppress

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from web import budget, live, watch, storage, pilots
from oneshot.clients import ConfigurationError, require_env
from web.limits import BodyLimitMiddleware
from web.build_judging import pipeline_digest

BUILD_FINGERPRINT = pipeline_digest()

STATIC = Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def lifespan(app):
    try: budget.recover_reservations()
    except budget.BudgetError: pass  # Samples remain available while accounting needs repair.
    async def janitor():
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(watch.cleanup)
    task = asyncio.create_task(janitor())
    try: yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError): await task

app = FastAPI(title="OneShot", lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware, max_bytes=3 * live.MAX_BYTES + 8 * 1024 * 1024)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def current_evidence(request: Request, call_next):
    if request.url.path.startswith("/static/uploads"):
        return JSONResponse({"detail": "Recording not found"}, 404)
    if request.method in ("POST", "DELETE", "PUT"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin recording changes are not allowed"}, 403)
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        return JSONResponse({"detail": "Invalid request length"}, 400)
    if declared > 3 * live.MAX_BYTES + 8 * 1024 * 1024:
        return JSONResponse({"detail": "Upload request is too large"}, 413)
    cookie = request.cookies.get("oneshot_owner", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie): cookie = secrets.token_urlsafe(32)
    token = storage.owner.set(cookie)
    try:
        response = await call_next(request)
    finally:
        storage.owner.reset(token)
    if request.cookies.get("oneshot_owner") != cookie:
        response.set_cookie("oneshot_owner", cookie, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=storage.RETENTION_SECONDS)
    if request.url.path in ("/", "/judge", "/pilot") or request.url.path.endswith(".json") or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/pilot")
def pilot_page():
    return FileResponse(STATIC / "pilot.html")


@app.get("/judge")
def judge_page():
    return FileResponse(STATIC / "judge.html")


@app.get("/api/procedures")
def procedures():
    return {"procedures": live.list_procedures()}


@app.post("/api/procedures")
def teach_procedure(reference: UploadFile = File(...), reference2: UploadFile | None = File(None),
                    title: str = Form("Packing procedure")):
    job_id = live.new_job_id()
    try:
        live.ensure_capacity()
        ref = live.save_upload(reference.file, reference.filename, job_id, "reference")
        second = live.save_upload(reference2.file, reference2.filename, job_id, "reference2") if reference2 and reference2.filename else None
        live.submit_reference(job_id, ref, title, second)
    except (live.UploadError, budget.BudgetError) as exc:
        storage.remove(job_id)
        raise HTTPException(429 if isinstance(exc, budget.BudgetError) else 400, str(exc)) from exc
    except (ValueError, OSError) as exc:
        storage.remove(job_id)
        raise HTTPException(400, str(exc)) from exc
    return {"job": job_id}


@app.get("/api/procedures/{job_id}")
def procedure_detail(job_id: str):
    authorize(job_id)
    try:
        _, proc = live.load_reference(job_id)
        return {"procedure": proc.to_dict()}
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/pilot-protocol")
def pilot_protocol():
    return pilots.protocol()


@app.post("/api/pilots")
def create_pilot(body: dict = Body(...)):
    reference = body.get("reference_job")
    if not isinstance(reference, str): raise HTTPException(400, "Choose a confirmed reference")
    authorize(reference)
    try: return pilots.create(reference, BUILD_FINGERPRINT, body.get("reference_participant", "A"), body.get("roles"))
    except (ValueError, KeyError) as exc: raise HTTPException(400, str(exc)) from exc


@app.get("/api/pilots/{pilot_id}")
def pilot_report(pilot_id: str):
    authorize(pilot_id)
    try: return pilots.report(pilot_id, BUILD_FINGERPRINT)
    except (ValueError, KeyError) as exc: raise HTTPException(400, str(exc)) from exc


@app.get("/api/pilots/{pilot_id}/export")
def pilot_export(pilot_id: str):
    authorize(pilot_id)
    return JSONResponse(pilots.report(pilot_id, BUILD_FINGERPRINT),
        headers={"Content-Disposition": f'attachment; filename="packing-trial-{pilot_id}.json"'})


@app.post("/api/pilots/{pilot_id}/cases/{case_id}")
def pilot_attempt(pilot_id: str, case_id: str, attempt: UploadFile = File(...), participant: str = Form("B"),
                  label_confirmed: bool = Form(False), retakes: int = Form(0)):
    authorize(pilot_id)
    try: return pilots.submit(pilot_id, case_id, participant, attempt.file, attempt.filename,
                              BUILD_FINGERPRINT, label_confirmed=label_confirmed, retakes=retakes)
    except budget.BudgetError as exc: raise HTTPException(429, str(exc)) from exc
    except (ValueError, OSError, KeyError) as exc: raise HTTPException(400, str(exc)) from exc


@app.post("/api/pilots/{pilot_id}/cases/{case_id}/timing")
def pilot_timing(pilot_id: str, case_id: str, body: dict = Body(...)):
    authorize(pilot_id)
    try: pilots.timing(pilot_id, case_id, body.get("manual_seconds"), body.get("assisted_seconds"))
    except (ValueError, KeyError) as exc: raise HTTPException(400, str(exc)) from exc
    return {"saved": True}


@app.get("/api/health")
def health() -> dict:
    """For the deployment check: is the server up, and how much of the spend cap is used."""
    try: require_env("MODEL_API_KEY"); configured = True
    except ConfigurationError: configured = False
    try: ledger = budget.status()
    except budget.BudgetError as exc: ledger = {"open": False, "error": str(exc)}
    return {"ok": True, "model_configured": configured, "retention_seconds": storage.RETENTION_SECONDS,
            "pipeline_sha256": BUILD_FINGERPRINT, **ledger}


def authorize(job_id):
    try: storage.authorize(job_id)
    except storage.AccessError as exc: raise HTTPException(404, "Unknown or expired check") from exc


@app.get("/api/assets/{job_id}/{filename}")
def private_asset(job_id: str, filename: str):
    try: return FileResponse(storage.asset(job_id, filename), media_type="video/mp4")
    except storage.AccessError as exc: raise HTTPException(404, "Unknown or expired recording") from exc


@app.get("/api/jobs/{job_id}/history")
def history(job_id: str):
    authorize(job_id)
    data = storage.record(job_id)
    return {"expires": data["expires"], "history": data["history"], "procedure": data.get("procedure")}


@app.get("/api/jobs")
def recent_checks():
    return {"checks": live.list_checks()}


@app.get("/api/jobs/{job_id}/report")
def download_report(job_id: str):
    authorize(job_id)
    data = storage.record(job_id)
    state = live.get(job_id) or {}
    if state.get("status") != "done":
        raise HTTPException(409, "Wait for the assessment to finish before downloading its report")
    return {"format": "oneshot-assessment-v1", "job": job_id, "created": data["created"],
            "procedure": data.get("procedure"), "machine_assessment": state.get("result"),
            "human_review": data.get("human_review"),
            "assessments": [{"at": event["at"], "event": event["event"],
                             "result": event.get("result") or (event.get("job") or {}).get("result")}
                            for event in data["history"]
                            if event.get("result") or (event.get("job") or {}).get("result")],
            "human_reviews": [{"at": event["at"], **event["human_review"]}
                              for event in data["history"] if event["event"] == "human_review_recorded"],
            "note": "Human reviews are separate from machine evidence. Media expires after 24 hours."}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    authorize(job_id)
    # Watch cleanup takes the session lock before touching live jobs: use the same order.
    with watch._lock:
        session = watch._sessions.get(job_id)
        if session:
            with session.lock:
                if session.in_flight or session.status == "finishing":
                    raise HTTPException(409, "Wait for active reads to finish before deleting the recording")
                session.status, session.pending = "cancelled", None
        watch._sessions.pop(job_id, None)
    with live._lock:
        found = live.get(job_id)
        if found and found["status"] in live.ACTIVE: raise HTTPException(409, "Wait for this check to finish before deleting it")
        storage.remove(job_id)
        live._jobs.pop(job_id, None); live._graphs.pop(job_id, None); live._pending.pop(job_id, None)
    return {"deleted": True}


@app.post("/api/jobs/{job_id}/procedure")
def confirm_procedure(job_id: str, body: dict = Body(...)):
    authorize(job_id)
    try: live.confirm(job_id, body)
    except (ValueError, TypeError, KeyError) as exc: raise HTTPException(400, str(exc)) from exc
    except budget.BudgetError as exc: raise HTTPException(429, str(exc)) from exc
    return {"confirmed": True}


@app.post("/api/jobs/{job_id}/followup")
def followup(job_id: str, attempt: UploadFile = File(...)):
    authorize(job_id)
    child = live.new_job_id()
    try:
        live.ensure_capacity()
        path = live.save_upload(attempt.file, attempt.filename, child, "attempt")
        live.submit_followup(job_id, child, path)
    except budget.BudgetError as exc:
        storage.remove(child)
        raise HTTPException(429, str(exc)) from exc
    except (ValueError, KeyError, OSError) as exc:
        storage.remove(child)
        raise HTTPException(400, str(exc)) from exc
    return {"job": child, "parent_job": job_id}


@app.post("/api/procedures/{job_id}/edit")
def edit_saved_procedure(job_id: str):
    authorize(job_id)
    try: live.edit_reference(job_id)
    except (ValueError, KeyError) as exc: raise HTTPException(400, str(exc)) from exc
    return {"job": job_id}


@app.post("/api/jobs/{job_id}/review")
def human_review(job_id: str, body: dict = Body(...)):
    authorize(job_id)
    if body.get("decision") not in ("accept", "reject", "needs_evidence") or not isinstance(body.get("note"), str) or not 1 <= len(body["note"].strip()) <= 2000:
        raise HTTPException(400, "Choose a review decision and provide a short reason")
    state = live.get(job_id)
    if not state or state["status"] != "done": raise HTTPException(409, "Wait for the machine assessment to finish")
    review = {"decision": body["decision"], "note": body["note"].strip(), "source": "human"}
    storage.update(job_id, event="human_review_recorded", human_review=review)
    # A human decision is never presented as machine corroboration.
    live._set(job_id, human_review=review)
    return review


@app.post("/api/check")
def check(attempt: UploadFile = File(...), reference: UploadFile | None = File(None),
          reference2: UploadFile | None = File(None), reference_demo: str = Form("pack")) -> dict:
    """Start a live check. Returns a job id to poll at /api/jobs/{id}."""
    try:
        live.ensure_capacity()
    except (budget.BudgetError, live.UploadError) as exc:
        raise HTTPException(429, str(exc)) from exc
    if reference_demo not in ("pack", "real"):
        raise HTTPException(400, "Unknown built-in reference.")
    job_id = live.new_job_id()
    att_path = ref_path = ref2_path = None
    try:
        att_path = live.save_upload(attempt.file, attempt.filename, job_id, "attempt")
        ref_path = (live.save_upload(reference.file, reference.filename, job_id, "reference")
                    if reference is not None and reference.filename else None)
        ref2_path = (live.save_upload(reference2.file, reference2.filename, job_id, "reference2")
                     if ref_path is not None and reference2 is not None and reference2.filename else None)
    except (ValueError, OSError) as exc:
        storage.remove(job_id)
        raise HTTPException(400, str(exc)) from exc
    try:
        live.submit(job_id, att_path, ref_path, ref2_path, reference_demo)
    except (live.UploadError, budget.BudgetError) as exc:
        storage.remove(job_id)
        raise HTTPException(429, str(exc)) from exc
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    authorize(job_id)
    found = live.get(job_id)
    if found is None:
        raise HTTPException(404, "unknown job")
    return found


@app.post("/api/watch")
def watch_start(mode: str = Form("replay"), demo: str | None = Form(None), reference_job: str | None = Form(None),
                attempt: UploadFile | None = File(None), reference_demo: str = Form("pack")) -> dict:
    """Start a near-live check: a sample or uploaded attempt replayed in real time, or the camera."""
    upload = None
    upload_id = live.new_job_id()
    if reference_job: authorize(reference_job)
    try:
        if mode == "replay" and demo is None and attempt is not None and attempt.filename:
            upload = live.save_upload(attempt.file, attempt.filename, upload_id, "watch")
        return watch.start(mode, demo=demo, upload=upload, reference_job=reference_job, reference_demo=reference_demo)
    except budget.BudgetError as exc:
        raise HTTPException(429, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, f"unknown {exc}") from exc
    finally:
        if upload is not None: storage.remove(upload_id)


@app.post("/api/watch/{sid}/chunk")
async def watch_chunk(sid: str, request: Request) -> dict:
    """The next piece of a camera recording (raw WebM bytes)."""
    authorize(sid)
    try:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > watch.MAX_CHUNK_BYTES:
                raise HTTPException(413, "The camera chunk exceeds its limit")
            body.extend(chunk)
        await asyncio.to_thread(watch.append, sid, bytes(body))
    except KeyError as exc:
        raise HTTPException(404, "unknown session") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/watch/{sid}/read")
def watch_read(sid: str, body: dict = Body(default={})) -> dict:
    """Read the attempt so far: {"upto": seconds} for a replay, {} for the camera."""
    authorize(sid)
    try:
        watch.read(sid, body.get("upto"))
    except KeyError as exc:
        raise HTTPException(404, "unknown session") from exc
    except live.UploadError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/watch/{sid}/finish")
def watch_finish(sid: str) -> dict:
    authorize(sid)
    try:
        watch.finish(sid)
    except KeyError as exc:
        raise HTTPException(404, "unknown session") from exc
    return {"ok": True}


@app.post("/api/watch/{sid}/cancel")
def watch_cancel(sid: str) -> dict:
    authorize(sid)
    try:
        watch.cancel(sid)
    except KeyError as exc:
        raise HTTPException(404, "unknown session") from exc
    return {"ok": True}


@app.get("/api/watch/{sid}")
def watch_state(sid: str) -> dict:
    authorize(sid)
    try:
        return watch.get(sid)
    except KeyError as exc:
        raise HTTPException(404, "unknown session") from exc


@app.post("/api/recheck")
def recheck(body: dict = Body(...)) -> dict:
    """Re-judge with rule changes: {"overrides": {rule id: strength}, "job": id} or {"demo": [ids]}."""
    if body.get("job"): authorize(body["job"])
    try:
        return live.recheck(body.get("overrides") or {}, job=body.get("job"), demo=body.get("demo"))
    except KeyError as exc:
        raise HTTPException(404, f"unknown attempt {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/watch/{sid}/events")
async def watch_events(sid: str, request: Request):
    authorize(sid)
    async def stream():
        previous = None
        while not await request.is_disconnected():
            try:
                storage.authorize(sid)
                state = watch.get(sid)
            except KeyError:
                yield 'event: expired\ndata: {}\n\n'
                return
            snapshot = json.dumps(state)
            if snapshot != previous:
                yield f"data: {snapshot}\n\n"
                previous = snapshot
            if state["status"] in ("done", "error", "cancelled"): return
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
