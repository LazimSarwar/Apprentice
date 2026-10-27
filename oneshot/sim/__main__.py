"""CLI: python -m oneshot.sim GRAPH.json [--video out.mp4]

Runs the task graph on the simulated Panda and prints an honest outcome report.

Procedure rules:
  --propose-procedure SPEC   write the derived ordering rules to an editable spec and exit
  --procedure SPEC           enforce only the confirmed rules from an (edited) spec

With --video, records the run (needs imageio; falls back to a note if unavailable).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import monitor
from .executor import Disturbance, execute_file
from ..schema import TaskGraph


def _propose(graph_path: Path, out: Path) -> None:
    graph = TaskGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
    spec = monitor.propose_spec(graph)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(f"proposed {len(spec['rules'])} ordering rule(s) (all unconfirmed) -> {out}")
    for i, r in enumerate(spec["rules"], start=1):
        print(f"  {i}. {r['means']}")
    print("Edit the file: set confirmed:true on the ones you accept (edit/add/delete as needed), "
          "then run with --procedure.")


def _load_rules(graph_path: Path, spec_path: Path):
    graph = TaskGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    for w in monitor.validate_spec(spec, graph):
        print(f"  ! {w}")
    rules = monitor.rules_from_spec(spec)
    print(f"enforcing {len(rules)} confirmed rule(s) from {spec_path.name}")
    return rules


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a task graph on the simulated Panda")
    parser.add_argument("graph", type=Path, help="task graph JSON from `python -m oneshot`")
    parser.add_argument("--video", type=Path, help="write an MP4 of the run")
    parser.add_argument(
        "--nudge", nargs=4, action="append", metavar=("OBJECT", "DX", "DY", "AFTER"),
        help="shove OBJECT by (DX, DY) metres after step AFTER; repeatable",
    )
    parser.add_argument(
        "--reorder", action="store_true",
        help="run the steps in reverse to show the temporal monitor catch a procedure violation",
    )
    parser.add_argument("--propose-procedure", type=Path, metavar="SPEC",
                        help="write the derived ordering rules to an editable spec and exit")
    parser.add_argument("--procedure", type=Path, metavar="SPEC",
                        help="enforce only the confirmed rules from an (edited) spec")
    args = parser.parse_args()

    if args.propose_procedure:
        _propose(args.graph, args.propose_procedure)
        return

    rules = _load_rules(args.graph, args.procedure) if args.procedure else None

    disturbances = [
        Disturbance(after_step=int(after), object=obj, dxy=(float(dx), float(dy)))
        for obj, dx, dy, after in (args.nudge or [])
    ]
    result = execute_file(args.graph, record=args.video is not None,
                          disturbances=disturbances, reorder=args.reorder, rules=rules)
    print(result.summary())

    if args.video and result.frames:
        try:
            import imageio.v3 as iio
            iio.imwrite(args.video, result.frames, fps=30)
            print("wrote", args.video)
        except Exception as exc:  # noqa: BLE001
            print(f"(video not written: {exc}; `pip install imageio[ffmpeg]` to enable)")


if __name__ == "__main__":
    main()
