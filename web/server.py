"""OneShot web app: pick a demonstration, see the task graph it learned, and run it on
the simulated arm — normally, out of order (procedure check), or with a mid-run shove
(recovery). The simulation runs live server-side; renders are cached per (task, mode).

Not a dashboard: the page is built around the agent's output — the learned plan, the
execution, and the verdict — for each demonstration.

Run:  python -m uvicorn web.server:app --port 8000   (from the app/ directory)
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from oneshot.schema import TaskGraph
from oneshot.sim.executor import Disturbance, execute

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
STATIC = ROOT / "static"
RENDERS = STATIC / "renders"
RENDERS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="OneShot")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_REGISTRY = json.loads((TASKS / "index.json").read_text(encoding="utf-8"))


def _graph(task: str) -> TaskGraph:
    path = TASKS / f"{task}.json"
    if task not in _REGISTRY or not path.exists():
        raise HTTPException(404, f"unknown task '{task}'")
    return TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))


def _graph_view(g: TaskGraph) -> dict:
    return {
        "objects": [{"id": o.id, "color": o.color, "shape": o.shape, "tracked": bool(o.tracked)}
                    for o in g.objects],
        "steps": [{"order": s.order, "action": s.action, "object": s.object,
                   "target": s.target, "relation": s.relation,
                   "start_s": float(s.start_s), "end_s": float(s.end_s)}
                  for s in sorted(g.steps, key=lambda s: s.order)],
        "final_relations": [{"a": r.a, "relation": r.relation, "b": r.b} for r in g.final_relations],
        "provider": g.meta.provider,
        "model": g.meta.model,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/tasks")
def tasks() -> dict:
    out = {}
    for tid, meta in _REGISTRY.items():
        out[tid] = {**meta, "graph": _graph_view(_graph(tid))}
    return out


def _result_view(r) -> dict:
    verdict = "SUCCESS" if r.success else ("INCOMPLETE" if not r.completed else "WRONG FINAL STATE")
    return {
        "verdict": verdict,
        "success": bool(r.success),
        "plan_notes": list(r.plan_notes),
        "steps": [{"order": int(s.order), "action": s.action, "object": s.object, "target": s.target,
                   "ok": bool(s.ok), "residual": round(float(s.residual), 3), "reason": s.reason}
                  for s in r.steps],
        "disturbances": list(r.disturbances),
        "repairs": [{"a": rp.a, "relation": rp.relation, "b": rp.b, "ok": bool(rp.ok), "round": int(rp.round)}
                    for rp in r.repairs],
        "final": [{"a": x.a, "relation": x.relation, "b": x.b, "holds": bool(x.holds)} for x in r.final],
        "procedure_ok": bool(r.procedure_ok),
        "procedure": [{"ok": bool(v.ok), "detail": v.detail} for v in r.procedure],
    }


def _run(task: str, mode: str):
    g = _graph(task)
    orders = sorted(s.order for s in g.steps)
    if mode == "normal":
        return execute(g, record=True)
    if mode == "reorder":
        return execute(g, record=True, order=list(reversed(orders)))
    if mode == "nudge":
        last = max(g.steps, key=lambda s: s.order)
        dist = Disturbance(after_step=last.order, object=last.object, dxy=(0.13, 0.08))
        return execute(g, record=True, disturbances=[dist])
    raise HTTPException(400, f"unknown mode '{mode}'")


@app.post("/api/run/{task}/{mode}")
def run(task: str, mode: str) -> JSONResponse:
    if task not in _REGISTRY:
        raise HTTPException(404, f"unknown task '{task}'")
    video = RENDERS / f"{task}_{mode}.mp4"
    result = _run(task, mode)
    if result.frames:
        try:
            import imageio.v3 as iio
            iio.imwrite(video, result.frames, fps=30)
        except Exception:  # noqa: BLE001 - video is optional; the verdict still returns
            video = None
    view = _result_view(result)
    view["video_url"] = f"/static/renders/{video.name}" if video and video.exists() else None
    return JSONResponse(view)
