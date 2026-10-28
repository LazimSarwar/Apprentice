"""Render short synthetic test clips (no camera needed), so the pipeline can run before
real footage exists.

  stack (default)  -> clips/synthetic.mp4 : red into a grey tray, blue stacked on red
  sort             -> clips/sort.mp4      : red into the left tray, blue into the right
  proc             -> clips/proc.mp4      : the reference procedure — red -> left bin,
                                            blue -> middle bin, green -> right bin, in order
  proc_swap        -> clips/proc_swap.mp4     : same moves, blue done first (wrong order)
  proc_skip        -> clips/proc_skip.mp4     : green never placed (skipped step)
  proc_wrongbin    -> clips/proc_wrongbin.mp4 : red and blue go to each other's bins

Usage:  python probes/make_test_clip.py [--task NAME]
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from common import ROOT

W, H = 1280, 720
BINS = {"left": (100, 430), "middle": (510, 430), "right": (920, 430)}   # top-left, 260x200
BIN_W, BIN_H, BLOCK = 260, 200, 100
STARTS = {"red": (1000, 80), "blue": (180, 80), "green": (590, 80)}


def moving(start: float, end: float, a: int, b: int) -> str:
    """ffmpeg expression: hold at a, move linearly to b between start and end seconds, then hold."""
    return f"'if(lt(t,{start}),{a},if(lt(t,{end}),{a}+(t-{start})/{end - start}*{b - a},{b}))'"


def bin_spot(name: str) -> tuple[int, int]:
    x, y = BINS[name]
    return x + (BIN_W - BLOCK) // 2, y + (BIN_H - BLOCK) // 2


def render_moves(path: Path, moves: dict, bins: dict | None = None, duration: float = 12.0) -> Path:
    """moves: colour -> (start_xy, end_xy or None, t0, t1). A move with end None stays put."""
    bins = bins if bins is not None else BINS
    boxes = ",".join(f"drawbox=x={x}:y={y}:w={BIN_W}:h={BIN_H}:color=gray:t=fill" for x, y in bins.values())
    chain, label = f"[0]{boxes}[bg];", "bg"
    colours = list(moves)
    for i, c in enumerate(colours):
        (sx, sy), end, t0, t1 = moves[c]
        if end is None:
            xe, ye = str(sx), str(sy)
        else:
            ex, ey = end
            xe, ye = moving(t0, t1, sx, ex), moving(t0, t1, sy, ey)
        out = "out" if i == len(colours) - 1 else f"s{i}"
        chain += f"[{label}][{i + 1}]overlay=x={xe}:y={ye}" + (f"[{out}];" if out != "out" else "")
        label = out
    inputs = ["-f", "lavfi", "-i", f"color=c=white:s={W}x{H}:d={duration}:r=30"]
    for c in colours:
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s={BLOCK}x{BLOCK}:d={duration}:r=30"]
    path.parent.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", chain,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def proc_moves(order: list[tuple[str, str | None]]) -> dict:
    """[(colour, bin or None), ...] in the order they happen, ~3.5 s apart."""
    slots = [(1.0, 3.0), (4.5, 6.5), (8.0, 10.0)]
    moves = {}
    for (colour, bin_name), (t0, t1) in zip(order, slots):
        moves[colour] = (STARTS[colour], bin_spot(bin_name) if bin_name else None, t0, t1)
    return moves


PROCEDURES = {
    "proc": [("red", "left"), ("blue", "middle"), ("green", "right")],
    "proc_swap": [("blue", "middle"), ("red", "left"), ("green", "right")],
    "proc_skip": [("red", "left"), ("blue", "middle"), ("green", None)],
    "proc_wrongbin": [("red", "middle"), ("blue", "left"), ("green", "right")],
}


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
    parser.add_argument("--task", choices=["stack", "sort", *PROCEDURES], default="stack")
    args = parser.parse_args()

    if args.task in PROCEDURES:
        path = render_moves(ROOT / "clips" / f"{args.task}.mp4", proc_moves(PROCEDURES[args.task]))
        print("wrote", path)
        return

    name, graph, colors = {"stack": stack_graph, "sort": sort_graph}[args.task]()
    target = ROOT / "clips" / name
    target.parent.mkdir(exist_ok=True)
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
