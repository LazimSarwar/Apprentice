"""Deploy the web app on Modal (https://modal.com) — one always-warm CPU container.

From app/:
    pip install modal && modal setup                        # once: sign in
    modal secret create oneshot-keys --from-dotenv .env     # once: the model API keys
    modal deploy deploy/modal_app.py                        # prints the public URL

Jobs and live sessions live in memory, so the app is pinned to a single container
(`max_containers=1`); the spend ledger sits on a Volume so a restart keeps the total.
"""

from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent          # app/
REMOTE = "/root/app"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(str(ROOT / "requirements.txt"))
    .add_local_dir(str(ROOT / "oneshot"), remote_path=f"{REMOTE}/oneshot", ignore=["**/__pycache__"])
    .add_local_dir(str(ROOT / "web"), remote_path=f"{REMOTE}/web",
                   ignore=["**/__pycache__", "static/uploads/**", "static/renders/**"])
)

app = modal.App("oneshot-web")
data = modal.Volume.from_name("oneshot-data", create_if_missing=True)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("oneshot-keys")],
    volumes={"/data": data},
    cpu=2.0,
    memory=2048,
    timeout=900,
    max_containers=1,
    min_containers=1,                 # no cold start for a judge opening the page
    scaledown_window=900,
)
@modal.concurrent(max_inputs=40)
@modal.asgi_app()
def web():
    import os
    import sys

    os.chdir(REMOTE)
    sys.path.insert(0, REMOTE)
    os.environ.setdefault("BUDGET_FILE", "/data/budget.json")
    from web.server import app as fastapi_app
    return fastapi_app
