"""Read SAM's bounding-box records without decoding unused pixel masks.

Uses the public ``ResponseFormat`` protocol from meta-sam-parser. The wire grammar
is documented in that package's SAM API parser (SAM License, credited in README).
Box coordinates, frame numbers and token framing are validated; dense mask pixels
are intentionally not decoded or used as evidence. Inclusive API right/bottom
coordinates become half-open coordinates, just like the official parser.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from meta_sam_parser import FrameReference, ParserFinish, SegmentationBoxRecord

_FRAME = re.compile(r"<([0-9]+)f(?:;[^>]*)?>")
_OBJECT = re.compile(r",?([0-9]+)(?=<\|)")
_TOKEN = re.compile(r"<\|([A-Za-z][A-Za-z0-9_.-]*)(?:;([^|]*))?\|>")
_INTEGER = re.compile(r"-?[0-9]+")


@dataclass(frozen=True)
class BoxResult:
    records: tuple[SegmentationBoxRecord, ...]
    outcome: object


def _fields(body: str | None) -> dict[str, str]:
    out = {}
    for field in (body or "").split(";"):
        if not field.strip():
            continue
        key, sep, value = field.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key in out:
            raise ValueError("Invalid or duplicate box field")
        out[key] = value
    return out


class BoxParser:
    def __init__(self):
        self._buffer = ""
        self._records: list[SegmentationBoxRecord] = []

    def push(self, chunk: str, *, emit: bool = True) -> tuple:
        lines = (self._buffer + chunk).split("\n")
        self._buffer = lines.pop()
        for line in lines:
            self._line(line.strip())
        # The application consumes the terminal result, not cumulative snapshots.
        return ()

    def finish(self, outcome):
        if self._buffer.strip():
            self._line(self._buffer.strip())
        self._buffer = ""
        return ParserFinish(events=(), result=BoxResult(tuple(self._records), outcome))

    def _line(self, line: str) -> None:
        if not line:
            return
        frame = _FRAME.match(line)
        if frame is None:
            if "<" in line or "|>" in line:
                raise ValueError("Malformed tracking frame")
            return  # A completed response can say no objects were detected.
        position = frame.end()
        while position < len(line):
            obj = _OBJECT.match(line, position)
            if obj is None:
                raise ValueError("Malformed tracking object")
            position = obj.end()
            tokens = {}
            while token := _TOKEN.match(line, position):
                name, body = token.groups()
                if name in tokens:
                    raise ValueError("Duplicate tracking token")
                tokens[name] = body
                position = token.end()
            if "box" not in tokens or "mask" not in tokens:
                raise ValueError("Tracking record must include box and mask tokens")
            values = _fields(tokens["box"])
            keys = ("x1", "y1", "x2", "y2", "w", "h")
            if any(not _INTEGER.fullmatch(values.get(k, "")) for k in keys):
                raise ValueError("Invalid tracking coordinates")
            left, top, right, bottom, width, height = (int(values[k]) for k in keys)
            if not (0 <= left <= right < width and 0 <= top <= bottom < height):
                raise ValueError("Tracking box falls outside its source frame")
            confidence = None
            if "c" in values:
                try:
                    number = float(values["c"])
                    if math.isfinite(number) and 0 <= number <= 1:
                        confidence = number
                except ValueError:
                    pass
            self._records.append(SegmentationBoxRecord(
                order=len(self._records), object_id=obj.group(1), frame=FrameReference(int(frame.group(1))),
                left=left, top=top, right=right + 1, bottom=bottom + 1, confidence=confidence))


class BoxVideoFormat:
    def create_parser(self) -> BoxParser:
        return BoxParser()
