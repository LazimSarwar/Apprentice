"""A spend cap for the public deployment.

Every model call the page triggers is charged here — extraction (its measured cost),
transcription (by audio length) and tracking (a flat estimate: the tracking API reports no
usage) — and once the total reaches SPEND_CAP_USD the page stops starting new checks. The
precomputed sample verdicts keep working after the cap, since re-checking them never calls a
model. The ledger is a JSON file (BUDGET_FILE) so a restart doesn't forget what was spent.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

CAP_USD = float(os.environ.get("SPEND_CAP_USD", "15"))
FILE = Path(os.environ.get("BUDGET_FILE") or (Path(__file__).resolve().parent.parent / "runs" / "budget.json"))
TRANSCRIBE_USD_PER_HOUR = 0.18
TRACK_EST_USD = float(os.environ.get("TRACK_EST_USD", "0.10"))   # per tracked attempt
RESERVE_USD = 0.50          # a new check needs at least this much left: one full check with tracking

_lock = threading.Lock()


class BudgetError(RuntimeError):
    pass


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"spent": 0.0, "events": []}


def spent() -> float:
    with _lock:
        return float(_load().get("spent", 0.0))


def charge(usd: float | None, what: str) -> None:
    if not usd:
        return
    with _lock:
        d = _load()
        d["spent"] = round(float(d.get("spent", 0.0)) + float(usd), 6)
        d["events"] = (d.get("events") or [])[-199:] + [{"t": round(time.time()), "usd": round(float(usd), 6),
                                                           "what": what}]
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(FILE)


def ensure(need: float = RESERVE_USD) -> None:
    """Refuse to start a model-calling check when the cap is (nearly) used up."""
    if CAP_USD - spent() < need:
        raise BudgetError("This public demo has used up its model budget for now. The sample verdicts above "
                          "still work — they're precomputed — and the repo runs the full checker locally.")


def status() -> dict:
    s = spent()
    return {"spent_usd": round(s, 4), "cap_usd": CAP_USD, "open": CAP_USD - s >= RESERVE_USD}
