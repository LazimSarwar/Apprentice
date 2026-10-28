"""OneShot web app server.

Serves the demo pages (built by `web.build_check` / `web.build_media`) and a live checker:
upload an attempt video — and optionally your own reference — and get the verdict.

Run from app/:  python -m uvicorn web.server:app --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
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
def check(attempt: UploadFile = File(...), reference: UploadFile | None = File(None)) -> dict:
    """Start a live check. Returns a job id to poll at /api/jobs/{id}."""
    job_id = live.new_job_id()
    try:
        att_path = live.save_upload(attempt.file, attempt.filename, job_id, "attempt")
        ref_path = (live.save_upload(reference.file, reference.filename, job_id, "reference")
                    if reference is not None and reference.filename else None)
    except live.UploadError as exc:
        raise HTTPException(400, str(exc)) from exc
    live.submit(job_id, att_path, ref_path)
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    found = live.get(job_id)
    if found is None:
        raise HTTPException(404, "unknown job")
    return found
