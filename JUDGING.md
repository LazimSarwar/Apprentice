# Judge walkthrough and evidence boundaries

## Three-minute demonstration

**0:00–0:15 — problem and provenance.** State that the application was built during the event and identify
the third-party model services, simulation assets and adapted recipes disclosed in the README. Explain the
problem: a final picture can look right while a required step was missed or performed too late.

**0:15–0:55 — real footage first.** Play the phone reference next to “Beside, not on.” Point to the learned
two-step procedure, wrong-placement timestamp and corrective action. Select a separate correct attempt.
Explain the partial tracking badge: seeing an object does not independently prove its 3D placement.

**0:55–1:15 — show an honest abstention.** Select “Green missing.” The model thinks a step was skipped;
the tracker has no evidence of that block, so acceptance is withheld. Show the nine-attempt summary and
the excluded reference self-check. Say “development footage,” not “unseen real-world accuracy.”

**1:15–2:00 — demonstrate which order matters.** Use the narrated packing example. Play the “liner first”
reason, then “Liner too late.” Change that specific rule to Free and show that its order failure changes.
Show a permitted card/charger swap. Explain that the packing scenes are generated tests.

**2:00–2:35 — working product.** Upload a downloaded phone sample with the phone reference selected, or
show a check already running. Distinguish the provisional model assessment from the later tracking result.
Completion time varies with provider latency; avoid promising an instant verdict.

**2:35–3:00 — measured scope and close.** Show the full results and test command. Explain that existing
generated cases are development replays, and any fresh batch is only new variations from the same renderer.
The practical next validation is new users, objects, cameras and real packing procedures with labels written
before testing. The simulated robot is an optional extension if time remains.

## Claims supported by the packaged evidence

- One reference produces structured steps; narration and a second reference can refine ordering rules.
- Real phone recordings can be compared with timestamps and corrective feedback.
- The current real development replay resolves seven of nine separate expected outcomes and abstains on two.
- The fresh generated batch matches 14/16 expected outcomes and retains two tracking timeouts as errors.
  Network deadlines were added after a stall; checker rules were not tuned against that batch.
- After the box-parser repair, the same sixteen development examples match 16/16 expected outcomes with
  zero tracking errors. Fourteen original extraction caches were replayed; the two prior failures were
  extracted and tracked again, correctly requiring review. The earlier failure report remains available.
- Four public upload checks completed in 35–70 seconds with zero tracking errors and working overlay videos:
  two correct runs passed, a wrong placement failed, and an occluded attempt required review. This is a small
  integration/availability check over three provided examples, not an unseen-accuracy benchmark.
- No incorrect real attempt in this small set is accepted. Abstention is not counted as a successful diagnosis.
- An identical recording used as both reference and attempt is excluded from evaluation scores.
- Tracker disagreement, absent tracking of a reported result, empty references and untimed required steps
  have offline regression checks.
- Real reversed stacks test wrong structure, not an isolated temporal-order violation.
- 2D boxes support approximate geometry and visibility. They cannot prove physical contact or safety.
- Near-live warning latency was measured on generated examples; a real webcam workflow remains unvalidated.

## Reproduce without spending model credits

```bash
python -m unittest discover -s tests -v
python -m web.build_judging --reports-only
python -m uvicorn web.server:app --port 8000
```

Inspect `web/static/media/real_results.json` for every real result, the excluded self-check, the checker
fingerprint and source-video hashes. Raw recordings and API keys are not included.
