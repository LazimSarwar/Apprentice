"""Validate the exact Linux/Python 3.12 image before deployment, without model calls.

Run: python -m modal run deploy/validate.py
"""
import modal

REMOTE = "/root/app"
validation_image = None
if modal.is_local():
    from deploy.modal_app import ROOT, image
    validation_image = image.add_local_dir(str(ROOT / "tests"), remote_path=f"{REMOTE}/tests", ignore=["**/__pycache__"])

app = modal.App("oneshot-release-validation")


@app.function(image=validation_image, timeout=600, cpu=1, memory=2048)
def verify():
    import os
    import subprocess
    import sys
    os.chdir(REMOTE)
    os.environ["PRIVATE_DATA_DIR"] = "/tmp/oneshot-validation/private"
    os.environ["BUDGET_FILE"] = "/tmp/oneshot-validation/budget.json"
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], check=True)
    sys.path.insert(0, REMOTE)
    from web.build_judging import pipeline_digest
    return {"python": sys.version, "pipeline_sha256": pipeline_digest(), "offline_tests": "passed"}


@app.local_entrypoint()
def main():
    import json
    result = verify.remote()
    target = ROOT / "runs/release-linux-validation.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
