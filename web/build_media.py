"""Build everything the web page shows, from the real pipeline outputs.

For each demonstration:
  demo  — the demonstration clip itself (web-encoded)
  seen  — the same clip with the tracker's boxes drawn on every object (what the AI saw)
  runs  — the simulated arm doing the task normally, from a shuffled layout, after a
          shove, and in the wrong order — each rendered to video with a plain-English result
  eval  — the measured rates from the seeded evaluation harness

Everything is written to web/static/media/ with a manifest.json the page reads.

Run from app/:  python -m web.build_media [--eval-trials 25]
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import threading
import uuid
from pathlib import Path

import imageio.v3 as iio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from oneshot.schema import TaskGraph
from oneshot.sim import monitor
from oneshot.sim.evaluate import evaluate, random_layout
from oneshot.sim.executor import Disturbance, execute, sanitize_plan
from oneshot.words import goal_past, goal_text, nice, step_text

APP = Path(__file__).resolve().parent.parent
MEDIA = APP / "web" / "static" / "media"

TASKS = {
    "real": {
        "title": "Real footage",
        "source": "A real stock video of a child stacking wooden cubes (cottonbro studio, Pexels).",
        "graph": "runs/3676968-uhd_2160_4096_25fps.graph.json",
        "clip": "runs/clips/3676968-uhd_2160_4096_25fps.720p.mp4",
    },
    "stack": {
        "title": "Stack",
        "source": "A generated test clip: the red block slides into the tray, then the blue one goes on top.",
        "graph": "runs/synthetic.graph.json",
        "clip": "runs/clips/synthetic.720p.mp4",
    },
    "sort": {
        "title": "Sort",
        "source": "A generated test clip: red goes to the left tray, blue to the right.",
        "graph": "runs/sort.graph.json",
        "clip": "runs/clips/sort.720p.mp4",
    },
}

COLORS = {
    "red": (217, 51, 51), "blue": (51, 89, 217), "green": (51, 178, 77), "yellow": (230, 204, 51),
    "purple": (128, 51, 160), "orange": (230, 131, 38), "brown": (128, 89, 46),
    "beige": (209, 184, 133), "pink": (230, 128, 166), "maroon": (128, 31, 51),
    "gray": (140, 140, 140), "grey": (140, 140, 140),
}
# --- video ------------------------------------------------------------------

def write_mp4(path: Path, frames, fps: float, crf: int = 27) -> None:
    """Stream RGB frames to ffmpeg as web-friendly H.264 (faststart, even dimensions)."""
    frames = iter(frames)
    first = np.ascontiguousarray(next(frames))
    h, w = first.shape[:2]
    from oneshot.media import MEDIA_TIMEOUT_SECONDS
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp.mp4")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{w}x{h}", "-r", f"{fps:.3f}", "-i", "-",
         "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-crf", str(crf), "-preset", "medium", "-movflags", "+faststart", str(temporary)],
        stdin=subprocess.PIPE,
    )
    errors = []
    def feed():
        try:
            proc.stdin.write(first.tobytes())
            for frame in frames: proc.stdin.write(np.ascontiguousarray(frame).tobytes())
        except Exception as exc:
            errors.append(exc)
        finally:
            try: proc.stdin.close()
            except OSError: pass
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        proc.wait(timeout=MEDIA_TIMEOUT_SECONDS)
        writer.join(timeout=1)
        if writer.is_alive() or errors or proc.returncode != 0:
            raise RuntimeError(f"Video rendering failed for {path.name}")
        temporary.replace(path)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=5)
        writer.join(timeout=1)
        temporary.unlink(missing_ok=True)


def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def overlay_frames(clip: Path, g: TaskGraph):
    """The demonstration with each tracked object boxed and labelled, frame by frame."""
    by_frame: dict[int, list] = {}
    for o in g.objects:
        if not o.track:
            continue
        rgb = COLORS.get((o.color or "").lower(), (255, 255, 255))
        for b in o.track.boxes:
            by_frame.setdefault(b.frame, []).append((b.box, rgb, nice(o.id)))
    font = line = size = None
    for i, frame in enumerate(iio.imiter(clip)):
        if font is None:                       # scale labels to the clip so they read at any size
            w = frame.shape[1]
            size = max(14, w // 42)
            font, line = _font(size), max(3, w // 320)
        img = Image.fromarray(frame)
        draw = ImageDraw.Draw(img)
        for (l, t, r, btm), rgb, label in by_frame.get(i, []):
            draw.rectangle([l, t, r, btm], outline=rgb, width=line)
            tw = draw.textlength(label, font=font)
            pad = size // 3
            ty = max(0, t - size - 2 * pad)
            draw.rectangle([l, ty, l + tw + 2 * pad, ty + size + 2 * pad], fill=rgb)
            draw.text((l + pad, ty + pad - 1), label, fill=(255, 255, 255), font=font)
        yield np.asarray(img)


# --- runs --------------------------------------------------------------------

def _outcome(r) -> dict:
    return {
        "success": bool(r.success),
        "completed": bool(r.completed),
        "goals": [{"text": goal_text(f.relation, f.a, f.b), "ok": bool(f.holds)} for f in r.final],
        "fixed": int(r.corrections),
        "procedure_ok": bool(r.procedure_ok),
        "violations": [f"{goal_past(v.after)} before {goal_past(v.before)}"
                       for v in r.procedure if not v.ok],
        "stopped": r.reason,
    }


def _render(name: str, task: str, r) -> str:
    path = MEDIA / f"{task}_{name}.mp4"
    write_mp4(path, r.frames, fps=30)
    return f"media/{path.name}"


def build_videos(task: str, spec: dict) -> tuple[TaskGraph, int, int]:
    """The demonstration clip and its tracking-overlay version."""
    g = TaskGraph.model_validate_json((APP / spec["graph"]).read_text(encoding="utf-8"))
    from oneshot.media import small_mp4
    source = Path(g.source)
    if not source.is_absolute(): source = APP / source
    clip = small_mp4(source, APP / 'runs/clips') if source.exists() else APP / spec["clip"]
    fps = float(iio.immeta(clip).get("fps", 25))
    _, h, w, _ = iio.improps(clip).shape
    print(f"[{task}] demo + tracking overlay")
    write_mp4(MEDIA / f"{task}_demo.mp4", iio.imiter(clip), fps)
    write_mp4(MEDIA / f"{task}_seen.mp4", overlay_frames(clip, g), fps)
    return g, w, h


def build_task(task: str, spec: dict, eval_trials: int) -> dict:
    g, w, h = build_videos(task, spec)

    steps, kept, _ = sanitize_plan(g)
    kept_orders = {s.order for s in steps}
    notes = [f"Skipped “{step_text(s)}” — the video never showed where it went."
             for s in g.steps if s.order not in kept_orders]
    notes += [f"Set aside “{goal_text(x.relation, x.a, x.b)}” — no step in the video achieves it."
              for x in g.final_relations if x not in kept]

    runs: dict[str, dict] = {}
    print(f"[{task}] robot: normal")
    r = execute(g, record=True)
    runs["normal"] = {"video": _render("robot", task, r), **_outcome(r)}

    print(f"[{task}] robot: shuffled layout")
    layout = random_layout(g, random.Random(1))
    r = execute(g, record=True, layout=layout)
    runs["shuffle"] = {"video": _render("shuffle", task, r), **_outcome(r)}

    print(f"[{task}] robot: bumped mid-run")
    last = max(steps, key=lambda s: s.order)
    r = execute(g, record=True,
                disturbances=[Disturbance(after_step=last.order, object=last.object, dxy=(0.10, 0.06))])
    runs["nudge"] = {"video": _render("nudge", task, r), "bumped": nice(last.object), **_outcome(r)}

    sanitized = g.model_copy(update={"steps": steps, "final_relations": kept})
    if monitor.derive_rules(sanitized):
        print(f"[{task}] robot: wrong order")
        r = execute(g, record=True, order=sorted(kept_orders, reverse=True))
        runs["order"] = {"video": _render("order", task, r), **_outcome(r)}
    else:
        runs["order"] = {"na": "This demonstration has only one goal, so there is no order to check."}

    print(f"[{task}] evaluation ({eval_trials} trials per check)")
    metrics = {m.name: [m.hits, m.total] for m in evaluate(g, trials=eval_trials, seed=0)}

    return {
        "id": task,
        "title": spec["title"],
        "source": spec["source"],
        "demo": f"media/{task}_demo.mp4",
        "seen": f"media/{task}_seen.mp4",
        "aspect": round(w / h, 4),
        "model": g.meta.model,
        "provider": g.meta.provider,
        "objects": [{"name": nice(o.id), "color": o.color, "tracked": bool(o.tracked)} for o in g.objects],
        "steps": [{"text": step_text(s), "start": s.start_s, "end": s.end_s}
                  for s in sorted(g.steps, key=lambda s: s.order)],
        "goals": [goal_text(x.relation, x.a, x.b) for x in kept],
        "notes": notes,
        "runs": runs,
        "eval": metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-trials", type=int, default=25)
    parser.add_argument("--only", choices=list(TASKS))
    parser.add_argument("--videos-only", action="store_true",
                        help="rebuild just the demo + tracking-overlay videos (fast)")
    args = parser.parse_args()
    MEDIA.mkdir(parents=True, exist_ok=True)

    if args.videos_only:
        for task, spec in TASKS.items():
            if not args.only or task == args.only:
                build_videos(task, spec)
        return

    manifest_path = MEDIA / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    for task, spec in TASKS.items():
        if args.only and task != args.only:
            continue
        manifest[task] = build_task(task, spec, args.eval_trials)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # keep the page's preferred order: real footage first
    ordered = {k: manifest[k] for k in TASKS if k in manifest}
    manifest_path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")
    print("wrote", manifest_path)


if __name__ == "__main__":
    main()
