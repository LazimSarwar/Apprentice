# OneShot judge walkthrough

Open `/judge` for the guided version. It labels every recording as real development footage or generated, plays reference and attempt together, and re-runs a rule change through the actual checker. It uses cached assessments explicitly; new uploads and camera reads call Meta services.

## Three-minute presentation

**0:00–0:35 — the practical problem.** Play the real phone reference beside the wrong-placement attempt. Explain: “A final picture alone cannot tell whether a required procedure was followed. OneShot learns the steps, checks another recording, and points to the problem and fix.” Show the timestamp. Say that these are builder-recorded development examples.

**0:35–1:20 — the differentiator.** Switch to the narrated generated packing example. The accessory went in before the required liner, even though the final arrangement looks right. Change only “liner before accessory” to Free. The deterministic checker changes the outcome without another model call. Restore the requirement. The human owns the specification.

**1:20–1:50 — evidence and uncertainty.** Play the hidden-card example. Show “review required.” Distinguish the video model's interpretation from independent object tracking. Two-dimensional boxes support visibility and approximate geometry; they do not establish physical contact or safety.

**1:50–2:30 — the working application.** Open the prepared saved reference, or teach a new one in advance. Show the complete step/final-goal editor and object types. Confirm it, reuse it for another recording or camera session, inspect the decision history, and download a report showing that human review is separate from machine verification. Recent checks recover accepted uploads; follow-ups keep their own URLs and confirmed procedure. Provider calls take time; never stage a cached response as live inference.

**2:30–3:00 — scope and impact plan.** Show 6/9 phone development outcomes resolved, 3 review requests, and zero observed false accepts. The same sixteen generated fixtures match 16/16 expected outcomes with two review cases. State that 218 offline tests pass locally and in the Linux/Python 3.12 deployment image. Show the baseline: on three generated order-only mistakes a final-picture check accepted all three and the rule checker accepted none. Say that the phone recordings contain no order-only mistake, and that in the saved recordings tracking added caution rather than corrections. Open the packing trial: labels and the procedure are frozen before inference, every first submission stays in the denominator. The independent second-person trial and measured benefit remain pending until actual recordings and timings exist.

## Reproduce

```bash
python -m unittest discover -s tests -v
python -m web.build_judging --reports-only
python -m modal run deploy/validate.py
python -m uvicorn web.server:app --port 8000
```

The current phone report is `web/static/media/real_results.json`; the older seven-of-nine report is `real_results_baseline.json`. The current generated replay is `current_validation.json`. Earlier repair and live-service artifacts retain their original source fingerprints and do not verify changed builds. `docs/evidence/release-linux-validation.json` records the image validation fingerprint.

## Five real packing development recordings

The final walkthrough step includes a separate P01–P05 development summary. It records the actual findings: the order, omission and placement challenges surfaced problems; correct recordings exposed containment and tracking failures. A later single-build run matched what all five recordings show, although P02's first reading reported one false problem, which closer looks now send to review, and it lost box tracking to a timeout. These five recordings informed fixes and span different builds, so do not claim five automatic passes, a common-build accuracy score, or independent validation. The covered-item check was also developed on P01 and P05, so their new results are not independent. Personal footage is not published. `packing_development.json` preserves sanitized observations, clip hashes and the two post-fix assessments with their original build fingerprint.

## Field trial

Record `REF.mp4` plus `P01.mp4`–`P09.mp4` using the [recording guide](docs/packing-trial-recording-guide.md). Use a second person, distinct objects except the identity challenge, continuous unedited takes, and human-confirmed labels before model output is inspected. Disclose retakes and coaching. Freeze the study in `/pilot` against a confirmed four-placement packing reference. Do not tune the checker to the recordings and then present those recordings as held out.

The report keeps expected outcomes, first results, reviews, pipeline errors, full denominators, clip hashes, source/plan hashes and model latency. Participant codes are self-reported. Optional human review times are self-timed, exclude model waiting and do not constitute a controlled impact study. Export before the retention period expires. Raw footage and owner-session credentials stay private.

## Provenance and limits

The application, schema, evidence checks, human confirmation, live workflow and trial protocol form the project contribution. Meta services provide transcription, video interpretation and tracking. Cookbook recipes, simulation assets, stock footage, dependencies and AI coding assistance are disclosed in README. The robot is a simulated optional extension. No physical robot, production accuracy, subsecond intervention, new second-person success, or measured time savings are claimed without evidence.
