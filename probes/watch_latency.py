"""Measure near-live checking the way the page uses it: against the running web app.

Each sample attempt is replayed in real time through /api/watch — a read of the video so far
every `--every` seconds, then the full check when the video ends — and for every warning we
record how long after the mistake it reached the page, plus false alarms on correct attempts
and whether the final verdict is right.

Usage (with the web app running on port 8000):
    python probes/watch_latency.py [--every 2] [--samples pack_order pack_ok ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"            # not "localhost": on Windows that costs ~2 s per request
EXPECT = {"pack_order": "fail", "pack_wrong_place": "fail", "pack_skip_liner": "fail", "pack_ok": "pass",
          "pack_skip_card": "fail", "pack_loose": "pass_notes"}


def _call(path: str, data: bytes | None = None, ctype: str = "application/json") -> dict:
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": ctype})
    return json.load(urllib.request.urlopen(req))


def replay(demo: str, every: float) -> dict:
    form = (f'--b\r\nContent-Disposition: form-data; name="mode"\r\n\r\nreplay\r\n'
            f'--b\r\nContent-Disposition: form-data; name="demo"\r\n\r\n{demo}\r\n--b--\r\n').encode()
    s = _call("/api/watch", form, "multipart/form-data; boundary=b")
    sid, duration = s["session"], s["duration"]
    t0, nxt, finished, caught = time.perf_counter(), every, False, {}
    while True:
        now = time.perf_counter() - t0
        while now >= nxt and nxt < duration:
            _call(f"/api/watch/{sid}/read", json.dumps({"upto": nxt}).encode())
            nxt += every
        if now >= duration and not finished:
            _call(f"/api/watch/{sid}/finish", b"{}")
            finished = True
        st = _call(f"/api/watch/{sid}")
        for a in st["alerts"]:
            caught.setdefault(a["id"], (round(time.perf_counter() - t0 - (a["time"] or 0), 1), a["severity"], a["message"]))
        if st["status"] in ("done", "error"):
            return {"caught": list(caught.values()), "verdict": (st["final"] or {}).get("verdict"),
                    "reads": st["reads"], "read_seconds": st["read_seconds"], "cost": st["cost"]}
        time.sleep(0.25)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--every", type=float, default=2.0)
    p.add_argument("--samples", nargs="+", default=list(EXPECT))
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    lags, false_alarms, right = [], 0, 0
    for demo in args.samples:
        r = replay(demo, args.every)
        ok = r["verdict"] == EXPECT[demo]
        right += ok
        fails = [c for c in r["caught"] if c[1] == "fail"]
        if EXPECT[demo] != "fail":
            false_alarms += len(fails)
        else:
            lags += [c[0] for c in fails]
        print(f"{demo:17} final {r['verdict']:10} {'ok' if ok else 'WRONG'}  {r['reads']} reads ~{r['read_seconds']}s  ${r['cost']}")
        for lag, sev, msg in r["caught"]:
            print(f"    +{lag:4}s  {sev:4}  {msg}")
    print(f"\nfinal verdicts right: {right}/{len(args.samples)}   false alarms on passing attempts: {false_alarms}")
    if lags:
        print(f"warning delay after the mistake: median {sorted(lags)[len(lags) // 2]}s, max {max(lags)}s "
              f"(n={len(lags)})")


if __name__ == "__main__":
    main()
