"""Double-check a reported problem before it reaches a person.

A video model answers a question about a whole recording confidently, even when it is
wrong, and asking the same question again tends to repeat the answer. A narrower question
about a short clip is a different look. Each reported wrong placement or skip gets two such
looks, one short and one wider, as a neutral multiple choice that does not say which answer
the first reading gave.

  both show the problem    it stays a failure
  both contradict it       it goes to review
  anything else            it goes to review

A closer look never turns a problem into a pass, because one reading cannot overrule
another on its own. If a look cannot run, the problem is reported as it was.
"""

from __future__ import annotations

import hashlib
import random
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from .schema import TaskGraph
from .words import nice, where

MAX_PROBLEMS = 4       # Problems checked per attempt; any others are reported as they were.
SHORT_BEFORE = 3.0     # Seconds of the short clip before the reported moment.
SHORT_AFTER = 3.0      # Seconds of the short clip after it.
WIDE_BEFORE = 8.0      # Seconds of the wide clip before the reported moment.
FINAL_SECONDS = 8.0    # A skip has no moment, so its short clip is the end of the recording.
MIN_SECONDS = 1.0      # A shorter clip cannot show a placement.
STOP_GESTURE = 1.0     # A hand often covers the lens to stop a phone recording in its last second.
DEADLINE_SECONDS = 60.0

PROMPT = """You are checking one moment of a recording of a tabletop task.
Watch this clip closely and judge by the LAST moment in which these objects are clearly visible.
Ignore a hand or arm that passes over them or covers the camera after that.

Objects:
{objects}

Question: where is the {subject} at that moment?
{options}

Return ONLY JSON, no prose: {{"choice": "A", "what_you_see": "one short sentence"}}"""


def to_check(report) -> list:
    """The failures a closer look can address, one per expected placement."""
    out, seen = [], set()
    for issue in report.issues:
        key = tuple(issue.check_key) if issue.check_key else None
        if issue.severity == "fail" and issue.kind in ("skipped", "wrong_place") and key and key not in seen:
            seen.add(key)
            out.append(issue)
    return out[:MAX_PROBLEMS]


def windows(issue, duration: float) -> list[tuple[float, float]]:
    """A short clip and a wider one, so a brief action and a later change can both be seen."""
    end = duration - STOP_GESTURE if duration - STOP_GESTURE >= MIN_SECONDS else duration
    if issue.kind == "skipped" or issue.time is None:
        start = max(0.0, issue.time or 0.0)
        # A step expected near the end leaves too little to watch, so widen to the whole clip.
        spans = [(max(0.0, end - FINAL_SECONDS), end), (start if end - start >= MIN_SECONDS else 0.0, end)]
    else:
        spans = [(max(0.0, issue.time - SHORT_BEFORE), min(end, issue.time + SHORT_AFTER)),
                 (max(0.0, issue.time - WIDE_BEFORE), end)]
    return [(round(s, 2), round(e, 2)) for s, e in spans if e - s >= MIN_SECONDS]


def _described(object_id: str, att: TaskGraph) -> str:
    found = next((o for o in att.objects if o.id == object_id), None)
    described = found.concept() if found is not None else ""
    name = nice(object_id)
    return f"{name} (looks like: {described})" if described and described.casefold() != name.casefold() else name


def _options(key, reported, salt: str) -> list[tuple[str, str]]:
    """(text, what choosing it means), shuffled so that position gives nothing away."""
    subject = nice(key[1])
    rows = [(f"The {subject} is {where(key[0], key[2])}", "expected")]
    if reported:
        rows.append((f"The {subject} is {where(reported[0], reported[2])}", "other"))
        rows.append((f"The {subject} is somewhere else", "other"))
    else:
        rows.append((f"The {subject} is not {where(key[0], key[2])}", "other"))
    random.Random(hashlib.sha256(salt.encode()).digest()).shuffle(rows)
    return [*rows, ("You cannot tell from this clip", "unclear")]


def build_question(issue, att: TaskGraph, span: tuple[float, float]) -> tuple[str, dict[str, str]]:
    key = tuple(issue.check_key)
    reported = tuple(issue.reported_key) if issue.reported_key else None
    options = _options(key, reported, f"{key}|{span}")
    letters = "ABCDE"
    named = list(dict.fromkeys(part for part in (*key[1:], *(reported[2:] if reported else ())) if part != "surface"))
    prompt = PROMPT.format(objects="\n".join(f"- {_described(n, att)}" for n in named), subject=nice(key[1]),
                           options="\n".join(f"{letters[i]}. {text}" for i, (text, _) in enumerate(options)))
    return prompt, {letters[i]: meaning for i, (_, meaning) in enumerate(options)}


def read_answer(text: str, meanings: dict[str, str]) -> tuple[str, str]:
    """(expected | other | unclear, what the model said it saw). Bad output counts as unclear."""
    from .extract import _loads
    try:
        data = _loads(text)
        choice = str(data.get("choice", "")).strip().upper()[:1]
        return meanings.get(choice, "unclear"), str(data.get("what_you_see", ""))[:200]
    except (ValueError, AttributeError, RuntimeError):  # ExtractionError is a RuntimeError.
        return "unclear", ""


def outcome(answers: list[str]) -> str:
    if len(answers) < 2 or any(a == "error" for a in answers):
        return "not_checked"
    if all(a == "other" for a in answers):
        return "confirmed"
    if all(a == "expected" for a in answers):
        return "disputed"
    return "unclear"


def run(report, att: TaskGraph, clip: Path, work: Path, ask: Callable[[Path, str], str] | None = None,
        cut: Callable | None = None, guard: Callable = nullcontext, deadline: float = DEADLINE_SECONDS) -> list[dict]:
    """Closer looks for every checkable problem. Never raises: a look that cannot run is recorded."""
    if ask is None:
        from .extract import ask_about_clip as ask
    if cut is None:
        from .media import segment as cut
    problems = to_check(report)
    if not problems:
        return []

    def look(issue, index: int, span: tuple[float, float]) -> dict:
        try:
            part = cut(clip, span[0], span[1], Path(work) / f"look_{index}_{span[0]}_{span[1]}.mp4")
            prompt, meanings = build_question(issue, att, span)
            with guard():
                answer, seen = read_answer(ask(part, prompt), meanings)
            return {"window": list(span), "answer": answer, "seen": seen}
        except Exception as exc:  # A failed look must never cost the assessment that already exists.
            return {"window": list(span), "answer": "error", "seen": type(exc).__name__}

    pool = ThreadPoolExecutor(max_workers=4)
    jobs = [(issue, [pool.submit(look, issue, i, span) for span in windows(issue, att.video.duration_s)])
            for i, issue in enumerate(problems)]
    wait([f for _, futures in jobs for f in futures], timeout=deadline)
    pool.shutdown(wait=False, cancel_futures=True)
    out = []
    for issue, futures in jobs:
        looks = [f.result() if f.done() and not f.cancelled() else {"window": None, "answer": "error", "seen": "Deadline"} for f in futures]
        out.append({"key": list(issue.check_key), "kind": issue.kind, "looks": looks,
                    "outcome": outcome([x["answer"] for x in looks])})
    return out


def attach(att: TaskGraph, looks: list[dict]) -> TaskGraph:
    if not looks:
        return att
    return att.model_copy(update={"meta": att.meta.model_copy(update={"second_looks": looks})})

