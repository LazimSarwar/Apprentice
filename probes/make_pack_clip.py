"""Render generated pack-out clips (top-down, flat shapes), optionally with spoken narration.

The procedure: a blue liner into the brown box, the red product onto the liner, the green
accessory into the box, the yellow card on top of the product. A gray plate beside the box
stands for another customer's order (the wrong place).

  ref       narrated reference ("Liner first, so the product doesn't get scratched." ...)
  ref2      a second correct way, no narration: card before accessory
  ok        reference order          -> should pass
  ok_alt    card before accessory    -> should pass (that order is free)
  loose     accessory before product -> should pass with a note (only shown, never said)
  order     accessory before liner   -> should fail ("liner first")
  skip_liner product straight into the box -> should fail (skipped liner)
  skip_card  no card                 -> should fail (skipped)
  wrong_place accessory onto the plate -> should fail (wrong place)
  hidden    a hand covers the box before the card goes in -> "can't verify", never a failure

Narration uses the Windows speech synthesizer (System.Speech), so `ref` needs Windows.

Usage:  python probes/make_pack_clip.py [--kind ref] [--seed N] [--out PATH]
"""

from __future__ import annotations

import argparse
import random
import subprocess
from pathlib import Path

from common import ROOT
from make_test_clip import moving

W, H = 1280, 720
BOX = (430, 330, 440, 340)                  # x, y, w, h — the brown box
PLATE = (990, 400, 240, 220)                # the gray plate (another order)
ITEMS = {                                    # name: (colour, w, h, start xy)
    "liner": ("blue", 280, 240, (40, 40)),
    "product": ("red", 120, 120, (420, 70)),
    "accessory": ("green", 70, 70, (640, 95)),
    "card": ("yellow", 90, 60, (820, 100)),
}
SPOTS = {"liner": (470, 380), "product": (550, 440), "accessory": (775, 430), "card": (565, 470),
         "plate": (1075, 475)}
REF = ["liner", "product", "accessory", "card"]
KINDS = {
    "ref": REF, "ref2": ["liner", "product", "card", "accessory"], "ok": REF,
    "ok_alt": ["liner", "product", "card", "accessory"], "loose": ["liner", "accessory", "product", "card"],
    "order": ["accessory", "liner", "product", "card"], "skip_liner": ["product", "accessory", "card"],
    "skip_card": ["liner", "product", "accessory"], "wrong_place": REF, "hidden": REF,
}
EXPECT = {"ref": "pass", "ref2": "pass", "ok": "pass", "ok_alt": "pass", "loose": "pass_notes",
          "order": "order", "skip_liner": "skipped", "skip_card": "skipped", "wrong_place": "wrong_place",
          "hidden": "no_false_fail"}
NARRATION = {
    "liner": "Liner first, so the product doesn't get scratched.",
    "product": "Product goes on the liner.",
    "accessory": "Charger in the box.",
    "card": "Thank-you card on top of the product.",
}


def plan(kind: str, rng: random.Random | None) -> tuple[list[tuple], float]:
    """[(item, start_xy, end_xy, t0, t1)] in layer order, and the clip duration."""
    order = KINDS[kind]
    t, moves = 1.0, {}
    for item in order:
        dur = 1.8 if rng is None else rng.uniform(1.4, 2.1)
        end = SPOTS["plate"] if kind == "wrong_place" and item == "accessory" else SPOTS[item]
        moves[item] = (end, round(t, 2), round(t + dur, 2))
        t += dur + (1.4 if rng is None else rng.uniform(0.9, 1.6))
    rows = []
    for item, (colour, w, h, (sx, sy)) in ITEMS.items():
        if rng is not None:
            sx, sy = sx + rng.randint(-25, 25), sy + rng.randint(-15, 25)
        end, t0, t1 = moves.get(item, (None, 0, 0))
        rows.append((item, (sx, sy), end, t0, t1))
    return rows, round(t + 0.6, 1)


def render(path: Path, rows: list[tuple], duration: float, hand: float | None = None) -> Path:
    bx, by, bw, bh = BOX
    px, py, pw, ph = PLATE
    chain = (f"[0]drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=saddlebrown:t=fill,"
             f"drawbox=x={px}:y={py}:w={pw}:h={ph}:color=gray:t=fill[bg];")
    label, inputs = "bg", ["-f", "lavfi", "-i", f"color=c=white:s={W}x{H}:d={duration}:r=30"]
    for i, (item, (sx, sy), end, t0, t1) in enumerate(rows):
        colour, w, h, _ = ITEMS[item]
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s={w}x{h}:d={duration}:r=30"]
        xe, ye = (str(sx), str(sy)) if end is None else (moving(t0, t1, sx, end[0]), moving(t0, t1, sy, end[1]))
        last = i == len(rows) - 1 and hand is None
        chain += f"[{label}][{i + 1}]overlay=x={xe}:y={ye}" + ("" if last else f"[s{i}];")
        label = f"s{i}"
    if hand is not None:                     # a hand-coloured block slides over the box and stays
        inputs += ["-f", "lavfi", "-i", f"color=c=peachpuff:s=470x370:d={duration}:r=30"]
        chain += (f"[{label}][{len(rows) + 1}]overlay=x={moving(hand, hand + 0.6, 1300, 415)}"
                  f":y={moving(hand, hand + 0.6, 720, 315)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", chain,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def speak(text: str, wav: Path) -> Path:
    """Windows text-to-speech into a WAV file."""
    wav.parent.mkdir(parents=True, exist_ok=True)
    safe = text.replace("'", "''")
    subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                    f"$s.Rate=1; $s.SetOutputToWaveFile('{wav}'); $s.Speak('{safe}'); $s.Dispose()"], check=True)
    return wav


def add_narration(video: Path, rows: list[tuple], out: Path, duration: float) -> Path:
    """Speak each step's line as it starts and mux the speech into the clip."""
    lines = [(t0, NARRATION[item]) for item, _, end, t0, _ in rows if end is not None]
    lines.sort()
    inputs, mix = ["-i", str(video)], ""
    for i, (t0, text) in enumerate(lines, start=1):
        wav = speak(text, ROOT / "runs" / "tts" / f"{out.stem}_{i}.wav")
        inputs += ["-i", str(wav)]
        ms = int(max(0.0, t0 - 0.3) * 1000)
        mix += f"[{i}]adelay={ms}|{ms}[a{i}];"
    mix += "".join(f"[a{i}]" for i in range(1, len(lines) + 1)) + f"amix=inputs={len(lines)}:normalize=0:duration=longest,apad=whole_dur={duration}[a]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", mix, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-t", str(duration), str(out)], check=True)
    return out


def make(kind: str, out: Path, seed: int | None = None) -> Path:
    rng = None if seed is None else random.Random(seed)
    rows, duration = plan(kind, rng)
    if kind == "hidden":                     # covered just before the card's step starts
        card_t0 = next(t0 for item, _, _, t0, _ in rows if item == "card")
        return render(out, rows, duration, hand=card_t0 - 0.8)
    if kind != "ref":
        return render(out, rows, duration)
    silent = render(out.with_name(out.stem + ".silent.mp4"), rows, duration)
    return add_narration(silent, rows, out, duration)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--kind", choices=list(KINDS), default="ref")
    p.add_argument("--seed", type=int, help="randomize start positions and timing")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    out = args.out or ROOT / "clips" / f"pack_{args.kind}.mp4"
    print("wrote", make(args.kind, out, args.seed))


if __name__ == "__main__":
    main()
