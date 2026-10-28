"""Probe 4: list the models your Fireworks account can serve, to find the Muse Spark Open slug.

Usage:  python probes/p4_fireworks_models.py   # needs FIREWORKS_API_KEY in .env

Prints every model id, highlighting ones that look like Spark / Muse / open-weight, so you
can set FIREWORKS_MODEL for `python -m oneshot CLIP --provider fireworks`.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI

from common import ROOT

load_dotenv(ROOT / ".env")
BASE = os.environ.get("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")


def main() -> None:
    key = os.environ.get("FIREWORKS_API_KEY", "").strip()
    if not key:
        raise SystemExit("FIREWORKS_API_KEY is not set in .env")
    api = OpenAI(base_url=BASE, api_key=key)
    ids = sorted(m.id for m in api.models.list().data)
    hits = [i for i in ids if any(w in i.lower() for w in ("spark", "muse", "open"))]
    print(f"{len(ids)} model(s) available on {BASE}")
    print("\nlikely Muse Spark Open candidates:")
    for i in (hits or ["  (none matched 'spark'/'muse'/'open' — see full list below)"]):
        print(f"  {i}")
    if not hits:
        for i in ids:
            print(f"  {i}")


if __name__ == "__main__":
    main()
