"""CLI: python -m oneshot VIDEO [--out graph.json]

Builds the evidence-linked task graph, prints a human summary, and writes the full
graph (including every tracked box) to JSON.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .clients import ROOT
from .graph import build_task_graph
from .schema import TaskGraph


def summarize(graph: TaskGraph) -> None:
    """Print a human-readable digest of a task graph (ASCII only, for any console)."""
    tracked = sum(o.tracked for o in graph.objects)
    print(f"source: {graph.source}")
    print(f"video : {graph.video.width}x{graph.video.height}, "
          f"{graph.video.duration_s:.1f}s @ {graph.video.fps} fps")
    print(f"objects ({tracked}/{len(graph.objects)} tracked):")
    for o in graph.objects:
        mark = "[x]" if o.tracked else "[ ]"
        span = f"{o.track.first_s}-{o.track.last_s}s, {o.track.visible_frames} frames" if o.track else "-"
        print(f"  {mark} {o.id} ({o.concept()}): {span}")
    print("steps:")
    for s in graph.steps:
        tgt = f" -> {s.target}" if s.target else ""
        rel = f" [{s.relation}]" if s.relation else ""
        print(f"  {s.order}. {s.start_s:.1f}-{s.end_s:.1f}s  {s.action} {s.object}{tgt}{rel}"
              f"  (evidence: {s.evidence.object_boxes_in_window} boxes)")
    if graph.final_relations:
        print("final:", ", ".join(f"{r.a} {r.relation} {r.b}" for r in graph.final_relations))
    print(f"cost  : ${graph.meta.extract_cost_usd}  "
          f"(extract {graph.meta.extract_seconds}s, track {graph.meta.track_seconds}s)")
    if graph.meta.warnings:
        print("warnings:")
        for w in graph.meta.warnings:
            print(f"  ! {w}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Video -> evidence-linked task graph")
    parser.add_argument("video", type=Path, help="demonstration clip (mp4/mov/...)")
    parser.add_argument("--out", type=Path, help="where to write the graph JSON")
    parser.add_argument("--work-dir", type=Path, help="scratch dir for shrunk clips")
    args = parser.parse_args()

    graph = build_task_graph(args.video, work_dir=args.work_dir)

    out = args.out or (ROOT / "runs" / f"{args.video.stem}.graph.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(graph.model_dump_json(indent=2) + "\n", encoding="utf-8")

    summarize(graph)
    print("wrote", out)


if __name__ == "__main__":
    main()
