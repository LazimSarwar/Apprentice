"""OneShot web app server.

Serves the demo pages (built by `web.build_check` / `web.build_media`) and a live checker:
upload an attempt video — and optionally your own reference (plus a second correct one) — and
get the verdict. Rule changes on the page are re-checked instantly via /api/recheck.

Run from app/:  python -m uvicorn web.server:app --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from web import live

STATIC = Path(__file__).resolve().parent / "static"
(STATIC / "uploads").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="OneShot")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/check")
def check(attempt: UploadFile = File(...), reference: UploadFile | None = File(None),
          reference2: UploadFile | None = File(None)) -> dict:
    """Start a live check. Returns a job id to poll at /api/jobs/{id}."""
    job_id = live.new_job_id()
    try:
        att_path = live.save_upload(attempt.file, attempt.filename, job_id, "attempt")
        ref_path = (live.save_upload(reference.file, reference.filename, job_id, "reference")
                    if reference is not None and reference.filename else None)
        ref2_path = (live.save_upload(reference2.file, reference2.filename, job_id, "reference2")
                     if ref_path is not None and reference2 is not None and reference2.filename else None)
    except live.UploadError as exc:
        raise HTTPException(400, str(exc)) from exc
    live.submit(job_id, att_path, ref_path, ref2_path)
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    found = live.get(job_id)
    if found is None:
        raise HTTPException(404, "unknown job")
    return found


@app.post("/api/recheck")
def recheck(body: dict = Body(...)) -> dict:
    """Re-judge with rule changes: {"overrides": {rule id: strength}, "job": id} or {"demo": [ids]}."""
    try:
        return live.recheck(body.get("overrides") or {}, job=body.get("job"), demo=body.get("demo"))
    except KeyError as exc:
        raise HTTPException(404, f"unknown attempt {exc}") from exc
