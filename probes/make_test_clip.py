"""Render short synthetic test clips (no camera needed), so the pipeline can run before
real footage exists.

  stack (default) -> clips/synthetic.mp4 : red moves into a grey tray, blue stacks on red.
  sort            -> clips/sort.mp4      : red moves into the left tray, blue into the right.

Usage:  python probes/make_test_clip.py [--task stack|sort]
"""

from __future__ import annotations

import argparse

from common import ROOT


def moving(start: float, end: float, a: int, b: int) -> str:
    """ffmpeg expression: hold at a, move linearly to b between start and end seconds, then hold."""
    return f"'if(lt(t,{start}),{a},if(lt(t,{end}),{a}+(t-{start})/{end - start}*{b - a},{b}))'"


def stack_graph() -> tuple[str, str, list[str]]:
    graph = (
        "[0]drawbox=x=820:y=380:w=320:h=220:color=gray:t=fill[bg];"
        f"[bg][1]overlay=x={moving(2, 5, 150, 900)}:y=450[a];"
        f"[a][2]overlay=x={moving(6, 9, 150, 900)}:y={moving(6, 9, 250, 350)}"
    )
    return "synthetic.mp4", graph, ["red", "blue"]


def sort_graph() -> tuple[str, str, list[str]]:
    graph = (
        "[0]drawbox=x=120:y=400:w=280:h=220:color=gray:t=fill,"
        "drawbox=x=880:y=400:w=280:h=220:color=gray:t=fill[bg];"
        f"[bg][1]overlay=x={moving(2, 5, 560, 210)}:y={moving(2, 5, 120, 460)}[a];"
        f"[a][2]overlay=x={moving(5, 8, 620, 970)}:y={moving(5, 8, 120, 460)}"
    )
    return "sort.mp4", graph, ["red", "blue"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=["stack", "sort"], default="stack")
    args = parser.parse_args()

    name, graph, colors = {"stack": stack_graph, "sort": sort_graph}[args.task]()
    target = ROOT / "clips" / name
    target.parent.mkdir(exist_ok=True)

    import subprocess
    inputs = ["-f", "lavfi", "-i", "color=c=white:s=1280x720:d=10:r=30"]
    for c in colors:
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s=100x100:d=10:r=30"]
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", graph, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
        check=True,
    )
    print("wrote", target)


if __name__ == "__main__":
    main()
