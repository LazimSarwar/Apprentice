"""Private, expiring job artifacts and an append-only decision history."""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("PRIVATE_DATA_DIR") or (Path(__file__).resolve().parent.parent / "runs" / "private"))
RETENTION_SECONDS = int(os.environ.get("RETENTION_SECONDS", "86400"))
MAX_STORAGE_BYTES = int(os.environ.get("MAX_STORAGE_BYTES", str(1024 * 1024 * 1024)))
owner = contextvars.ContextVar("recording_owner", default="local")
_lock = threading.RLock()
_ID = re.compile(r"^[a-f0-9]{12,32}$")


class AccessError(KeyError):
    pass


def directory(job: str) -> Path:
    if not _ID.fullmatch(job):
        raise AccessError(job)
    return ROOT / job


def _owner_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def create(job: str) -> Path:
    with _lock:
        cleanup()
        path = directory(job)
        if not path.exists():
            _write(path / "record.json", {"owner": _owner_hash(owner.get()), "created": time.time(),
                "expires": time.time() + RETENTION_SECONDS, "status": "created", "history": []})
        else:
            authorize(job)
        return path


def record(job: str) -> dict:
    try:
        data = json.loads((directory(job) / "record.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AccessError(job) from exc
    if data["expires"] <= time.time():
        raise AccessError(job)
    return data


def authorize(job: str) -> None:
    data = record(job)
    if not secrets.compare_digest(data["owner"], _owner_hash(owner.get())):
        raise AccessError(job)


def update(job_id: str, *, event: str | None = None, **fields) -> None:
    with _lock:
        data = record(job_id)
        data.update(fields)
        if event:
            data["history"].append({"at": time.time(), "event": event, **fields})
        _write(directory(job_id) / "record.json", data)


def asset(job: str, filename: str) -> Path:
    authorize(job)
    if Path(filename).name != filename or not filename.endswith(".mp4"):
        raise AccessError(job)
    path = directory(job) / "encoded" / filename
    if not path.is_file():
        raise AccessError(job)
    return path


def remove(job: str) -> None:
    with _lock:
        path = directory(job).resolve()
        if path.parent != ROOT.resolve():
            raise AccessError(job)
        if path.exists():
            shutil.rmtree(path)


def cleanup() -> set[str]:
    """Only remove verified children of our dedicated private root."""
    removed = set()
    with _lock:
        if not ROOT.exists():
            return removed
        for path in ROOT.iterdir():
            if not path.is_dir() or not _ID.fullmatch(path.name):
                continue
            try:
                expired = json.loads((path / "record.json").read_text(encoding="utf-8"))["expires"] <= time.time()
            except (OSError, ValueError, KeyError):
                continue  # Do not guess ownership or retention for unrecognised files.
            if expired:
                remove(path.name)
                removed.add(path.name)
    return removed


def ensure_space(additional: int = 0) -> None:
    cleanup()
    used = sum(p.stat().st_size for p in ROOT.rglob("*") if p.is_file()) if ROOT.exists() else 0
    if used + additional > MAX_STORAGE_BYTES:
        raise ValueError("Recording storage is full. Delete an older check or try again after it expires.")
