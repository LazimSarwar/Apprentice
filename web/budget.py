"""Persistent admission reservations; unknown provider work stays accounted."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from oneshot.clients import usage_observer

CAP_USD = float(os.environ.get("SPEND_CAP_USD", "15"))
FILE = Path(os.environ.get("BUDGET_FILE") or (Path(__file__).resolve().parent.parent / "runs" / "budget.json"))
TRANSCRIBE_USD_PER_HOUR = 0.18
TRACK_EST_USD = float(os.environ.get("TRACK_EST_USD", "0.10"))
RESERVE_USD = float(os.environ.get("MODEL_RESERVE_USD", "0.50"))

_lock = threading.RLock()


class BudgetError(RuntimeError):
    pass


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or not (0 <= float(d["spent"]) < float("inf")):
            raise ValueError("Invalid spend")
        d["spent"] = float(d["spent"])
        d.setdefault("reserved", {})
        if not isinstance(d["reserved"], dict) or any(
                not (0 <= float(v) < float("inf")) for v in d["reserved"].values()):
            raise ValueError("Invalid reservations")
        d["reserved"] = {key: float(value) for key, value in d["reserved"].items()}
        return d
    except FileNotFoundError:
        return {"spent": 0.0, "reserved": {}, "events": []}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BudgetError("The budget ledger needs administrator review; model calls are paused.") from exc


def _write(d):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(FILE)


def _event(d, usd, what, estimated=False):
    d["spent"] = round(float(d["spent"]) + usd, 6)
    d["events"] = (d.get("events") or [])[-199:] + [
        {"t": round(time.time()), "usd": round(usd, 6), "what": what, "estimated": estimated}]


def spent() -> float:
    with _lock:
        return float(_load()["spent"])


def charge(usd: float | None, what: str) -> None:
    if usd is None: return
    if not math.isfinite(usd) or usd < 0: raise ValueError("Invalid charge")
    with _lock:
        d = _load(); _event(d, usd, what); _write(d)


def ensure(need: float = RESERVE_USD) -> None:
    """Refuse to start a model-calling check when the cap is (nearly) used up."""
    with _lock:
        if not math.isfinite(CAP_USD) or CAP_USD < 0 or not math.isfinite(need) or need < 0:
            raise BudgetError("Invalid model budget configuration")
        d = _load()
        if CAP_USD - d["spent"] - sum(d["reserved"].values()) < need:
            raise BudgetError("The model budget is currently reserved or exhausted. Sample verdicts still work.")


@dataclass
class Meter:
    amount: float
    cost: float = 0.0
    observed: bool = False
    estimated: bool = False
    unknown: bool = False

    def observe(self, usd, *, estimated=False):
        self.observed = True
        self.unknown |= usd is None
        self.estimated |= estimated
        if usd is not None:
            if not math.isfinite(usd) or usd < 0: raise ValueError("Invalid provider usage")
            self.cost += usd
        if self.cost > self.amount:
            raise BudgetError("Provider usage exceeded its reserved estimate; further calls are paused.")


@contextmanager
def call(what: str, maximum: float = RESERVE_USD):
    if not math.isfinite(maximum) or maximum < 0: raise ValueError("Invalid reservation")
    key = uuid.uuid4().hex
    with _lock:
        ensure(maximum)
        d = _load(); d["reserved"][key] = maximum; _write(d)
    meter = Meter(maximum)
    token = usage_observer.set(meter.observe)
    failed = True
    try:
        yield meter
        failed = False
    finally:
        usage_observer.reset(token)
        with _lock:
            d = _load(); d["reserved"].pop(key, None)
            uncertain = meter.unknown or failed or not meter.observed
            amount = max(maximum, meter.cost) if uncertain else meter.cost
            _event(d, amount, what, estimated=uncertain or meter.estimated)
            _write(d)


def recover_reservations():
    """At startup only, account for interrupted calls that may have been billed."""
    with _lock:
        d = _load()
        for value in d["reserved"].values(): _event(d, value, "interrupted request reservation", True)
        d["reserved"] = {}; _write(d)


def status() -> dict:
    with _lock:
        d = _load(); reserved = sum(d["reserved"].values())
        return {"spent_usd": round(d["spent"], 4), "reserved_usd": round(reserved, 4),
                "cap_usd": CAP_USD, "open": CAP_USD-d["spent"]-reserved >= RESERVE_USD,
                "accounting": "reserved estimates; provider bills remain authoritative"}
