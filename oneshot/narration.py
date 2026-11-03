"""What the person recording the reference *said*: reasons, critical steps, order cues.

1. pull the audio track out of the reference video (mono 16 kHz WAV);
2. Muse Voice Transcribe turns it into timed turns ("Liner first, so the product doesn't
   get scratched." at 0.9 s);
3. Muse Spark reads the turns next to the learned steps (each with its time window) and
   returns, per step, the exact quote and the reason given, whether the speaker marked it
   as critical, and any spoken order constraints ("first", "before", "last", "any order").

The model may only quote what was said: every quote is checked against the transcript and
dropped if it isn't there, so a reason can never be invented.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx2 as httpx
from openai import OpenAI

from .clients import META_BASE_URL, MODEL_TIMEOUT_SECONDS, require_env, spark_cost_usd, record_usage
from .media import MEDIA_TIMEOUT_SECONDS, PROBE_TIMEOUT_SECONDS, source_digest

TRANSCRIBE_URL = f"{META_BASE_URL}/asr/transcribe"
TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"
SPARK_MODEL = "muse-spark-1.3"


@dataclass
class Turn:
    start: float
    end: float
    text: str


@dataclass
class StepNote:
    step: int                       # 1-based index into the reference's milestones
    quote: str | None = None
    reason: str | None = None
    critical: bool = False


@dataclass
class OrderNote:
    before: int
    after: int
    quote: str


@dataclass
class Narration:
    turns: list[Turn]
    steps: list[StepNote] = field(default_factory=list)
    order: list[OrderNote] = field(default_factory=list)
    any_order: list[tuple[int, int, str]] = field(default_factory=list)
    seconds: float | None = None
    cost_usd: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Narration":
        return cls(turns=[Turn(**t) for t in d.get("turns", [])],
                   steps=[StepNote(**s) for s in d.get("steps", [])],
                   order=[OrderNote(**o) for o in d.get("order", [])],
                   any_order=[tuple(x) for x in d.get("any_order", [])],
                   seconds=d.get("seconds"), cost_usd=d.get("cost_usd"))


# --- audio + transcription -----------------------------------------------------------

def has_audio(video: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, check=True, timeout=PROBE_TIMEOUT_SECONDS).stdout
    return bool(out.strip())


def extract_audio(video: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / f"{video.stem}.{source_digest(video)[:20]}.16k.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                    "-sample_fmt", "s16", str(wav)], check=True, timeout=MEDIA_TIMEOUT_SECONDS)
    return wav


def transcribe(wav: Path) -> list[Turn]:
    """Timed turns from Muse Voice Transcribe (one HTTP request, ENDPOINTING mode)."""
    request = {"mode": "ENDPOINTING", "model": TRANSCRIBE_MODEL, "audioEncoding": "WAV"}
    r = httpx.post(TRANSCRIBE_URL, headers={"Authorization": f"Bearer {require_env('MODEL_API_KEY')}"},
                   files={"request": (None, json.dumps(request), "application/json"),
                          "audio": (wav.name, wav.read_bytes())}, timeout=MODEL_TIMEOUT_SECONDS)
    if r.status_code != 200:
        raise RuntimeError(f"transcription failed [{r.status_code}]: {r.text[:300]}")
    body = r.json()
    turns = [Turn(round((t.get("startMs") or 0) / 1000, 2), round((t.get("endMs") or 0) / 1000, 2),
                  (t.get("transcript") or "").strip()) for t in body.get("turns") or []]
    for t in turns:
        t.text = _unstutter(t.text)
    if not turns and body.get("transcript"):
        turns = [Turn(0.0, (body.get("audioDurationMs") or 0) / 1000, body["transcript"].strip())]
    return [t for t in turns if t.text]


def _unstutter(text: str) -> str:
    """Drop a partial phrase glued to its own restart ('charger inCharger in the box.') and a
    word heard twice ('in the box box')."""
    parts = re.split(r"(?<=[a-z])(?=[A-Z])", text)
    out = parts[0]
    for p in parts[1:]:
        out = p if p.lower().startswith(out.strip().lower()) else out + p
    return re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", out, flags=re.IGNORECASE)


def listen(video: Path, work_dir: Path) -> list[Turn] | None:
    """What was said in a video, as timed turns — or None if it has no audio or speech."""
    if not has_audio(video):
        return None
    return transcribe(extract_audio(video, work_dir)) or None


def spoken_text(turns: list[Turn] | None) -> str | None:
    return " ".join(t.text for t in turns) if turns else None


# --- reading the narration against the steps -----------------------------------------

PROMPT = """Someone recorded a video of a procedure done right and talked while doing it.
Here are the steps that were seen in the video, with when each happened, and what they said.

STEPS
{steps}

WHAT THEY SAID (seconds from the start)
{turns}

Return ONLY JSON, no prose:
{{"steps": [{{"step": 1, "quote": "exact words said about this step, or null",
             "reason": "why it matters, in their words, short, or null",
             "critical": true}}],
  "order": [{{"before": 1, "after": 2, "quote": "exact words"}}],
  "any_order": [{{"steps": [3, 4], "quote": "exact words"}}]}}

Rules:
- Use only what was actually said. Quotes must be copied exactly from the transcript.
- A step is critical only if they said it matters, gave a consequence of getting it wrong,
  or used words like "must", "always", "never", "important", "make sure", "first".
- "X first" means X comes before every other step: give one order entry per later step.
- "X last" means every other step comes before X. "before"/"after"/"then" give one entry.
- Only add any_order when they said the order doesn't matter.
- Include every step in "steps", with nulls if nothing was said about it."""


def _loads(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in narration reply: {text[:200]!r}")
    return json.loads(m.group(0))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("’", "'").replace("'", "")).strip()


def read_narration(turns: list[Turn], steps: list[tuple[str, float, float]]) -> Narration:
    """Map what was said onto the steps. `steps` = (text, start_s, end_s) in reference order."""
    said = " ".join(t.text for t in turns)
    step_lines = "\n".join(f"{i}. {text}  ({s:.1f}s - {e:.1f}s)" for i, (text, s, e) in enumerate(steps, 1))
    turn_lines = "\n".join(f"[{t.start:.1f}s - {t.end:.1f}s] {t.text}" for t in turns)
    api = OpenAI(base_url=META_BASE_URL, api_key=require_env("MODEL_API_KEY"),
                 timeout=MODEL_TIMEOUT_SECONDS, max_retries=0)
    resp = api.responses.create(model=SPARK_MODEL, reasoning={"effort": "low"},
                                input=PROMPT.format(steps=step_lines, turns=turn_lines))
    record_usage(spark_cost_usd(resp.usage))
    data = _loads(resp.output_text)
    n = len(steps)

    def real(q) -> str | None:                         # keep a quote only if it was said
        return q if isinstance(q, str) and q.strip() and _norm(q) in _norm(said) else None

    notes = []
    for s in data.get("steps", []):
        i = s.get("step")
        if isinstance(i, int) and 1 <= i <= n:
            quote = real(s.get("quote"))
            notes.append(StepNote(i, quote, (s.get("reason") or None) if quote else None,
                                  bool(s.get("critical")) and quote is not None))
    order = [OrderNote(o["before"], o["after"], real(o.get("quote")))
             for o in data.get("order", [])
             if isinstance(o.get("before"), int) and isinstance(o.get("after"), int)
             and 1 <= o["before"] <= n and 1 <= o["after"] <= n and o["before"] != o["after"]
             and real(o.get("quote"))]
    free = []
    for a in data.get("any_order", []):
        ids = [i for i in a.get("steps", []) if isinstance(i, int) and 1 <= i <= n]
        if len(ids) >= 2 and real(a.get("quote")):
            free += [(x, y, a["quote"]) for k, x in enumerate(ids) for y in ids[k + 1:]]
    return Narration(turns=turns, steps=notes, order=order, any_order=free,
                     cost_usd=spark_cost_usd(resp.usage))


def narrate(turns: list[Turn] | None, steps: list[tuple[str, float, float]]) -> Narration | None:
    """Read already-transcribed turns against the reference's steps (None if nothing was said)."""
    if not turns:
        return None
    start = time.perf_counter()
    n = read_narration(turns, steps)
    n.seconds = round(time.perf_counter() - start, 2)
    return n
