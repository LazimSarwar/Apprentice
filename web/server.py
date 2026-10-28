"""OneShot web app server.

The page tells the story for each demonstration — watch, understand, do, check — using
media and results produced by the real pipeline (`python -m web.build_media`). The server
only serves those files, so the same page can also be hosted statically.

Run from app/:  python -m uvicorn web.server:app --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="OneShot")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
