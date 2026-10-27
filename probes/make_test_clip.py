"""Render a 10 s synthetic test clip (no camera needed): a red block moves into a grey tray,
then a blue block is stacked on it. Lets the probes run before real footage exists.

Usage:  python probes/make_test_clip.py   ->  clips/synthetic.mp4
"""

from __future__ import annotations

import subprocess

from common import ROOT


def moving(start: float, end: float, a: int, b: int) -> str:
    """ffmpeg expression: hold at a, move linearly to b between start and end seconds, then hold."""
    return f"'if(lt(t,{start}),{a},if(lt(t,{end}),{a}+(t-{start})/{end - start}*{b - a},{b}))'"


def main() -> None:
    target = ROOT / "clips" / "synthetic.mp4"
    target.parent.mkdir(exist_ok=True)
    graph = (
        "[0]drawbox=x=820:y=380:w=320:h=220:color=gray:t=fill[bg];"
        f"[bg][1]overlay=x={moving(2, 5, 150, 900)}:y=450[a];"
        f"[a][2]overlay=x={moving(6, 9, 150, 900)}:y={moving(6, 9, 250, 350)}"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=white:s=1280x720:d=10:r=30",
            "-f", "lavfi", "-i", "color=c=red:s=100x100:d=10:r=30",
            "-f", "lavfi", "-i", "color=c=blue:s=100x100:d=10:r=30",
            "-filter_complex", graph, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
        ],
        check=True,
    )
    print("wrote", target)


if __name__ == "__main__":
    main()
