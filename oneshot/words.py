"""Plain-English wording for objects, steps and goals, shared by the checker and the web page."""

from __future__ import annotations

_MODIFIERS = ("left", "right", "top", "middle", "bottom", "extra", "front", "back", "center")
_IN = {"inside": "in", "on": "on", "near": "next to"}
SURFACE = "surface"        # the work surface, whatever the video model calls it (see procedure.canonical_steps)


def nice(obj_id: str | None) -> str:
    """'gray_tray_left' -> 'left gray tray', 'purple_block_top' -> 'top purple block'."""
    if not obj_id:
        return ""
    parts = obj_id.split("_")
    mods = [p for p in parts if p in _MODIFIERS]
    rest = [p for p in parts if p not in _MODIFIERS]
    return " ".join(mods + rest)


def where(relation: str | None, target: str | None) -> str:
    """'in the left gray tray'."""
    return f"{_IN.get(relation or '', 'to')} the {nice(target)}"


def step_text(s) -> str:
    o, t, rel = nice(s.object), nice(s.target) if s.target else None, s.relation
    if s.action in ("place", "stack") and s.target == SURFACE:
        return f"Puts down the {o}"
    if s.action == "push":
        return f"Slides the {o} {'into' if rel == 'inside' else 'onto' if rel == 'on' else 'over to'} the {t}"
    if s.action == "stack":
        return f"Stacks the {o} on the {t}"
    if s.action == "place":
        if not t:
            return f"Puts down the {o}"
        return f"Places the {o} {_IN.get(rel or '', 'by')} the {t}"
    if s.action == "pick":
        return f"Picks up the {o}" + (f" and moves it to the {t}" if t else "")
    return f"{s.action} {o}"


def goal_text(rel: str, a: str, b: str) -> str:
    word = {"inside": "is in", "on": "is on", "near": "is next to"}.get(rel, rel)
    return f"The {nice(a)} {word} the {nice(b)}"


def goal_past(key) -> str:
    rel, a, b = key
    word = {"inside": "was in", "on": "was on", "near": "was next to"}.get(rel, rel)
    return f"the {nice(a)} {word} the {nice(b)}"


def clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = max(0.0, float(seconds))
    return f"{int(s // 60)}:{int(s % 60):02d}"


def imperative(step: str) -> str:
    """'Places the red block in the left tray' -> 'Place the red block in the left tray'."""
    first, _, rest = step.partition(" ")
    if first.endswith("es") and first[:-2].endswith(("sh", "ch", "x")):
        first = first[:-2]
    elif first.endswith("s"):
        first = first[:-1]
    return f"{first} {rest}".strip()


def into(relation: str | None, target: str | None) -> str:
    """'into the left gray tray' / 'onto the product' — for instructions."""
    word = {"inside": "into", "on": "onto", "near": "next to"}.get(relation or "", "to")
    return f"{word} the {nice(target)}"
