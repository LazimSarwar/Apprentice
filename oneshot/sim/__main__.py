"""CLI: python -m oneshot.sim GRAPH.json [--video out.mp4]

Runs the task graph on the simulated Panda and prints an honest outcome report.
With --video, records the run (needs imageio; falls back to a note if unavailable).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .executor import Disturbance, execute_file


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
    args = parser.parse_args()

    disturbances = [
        Disturbance(after_step=int(after), object=obj, dxy=(float(dx), float(dy)))
        for obj, dx, dy, after in (args.nudge or [])
    ]
    result = execute_file(args.graph, record=args.video is not None,
                          disturbances=disturbances, reorder=args.reorder)
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
