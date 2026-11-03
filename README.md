# OneShot

**Show it once. Check every attempt.** Record a procedure done right — once. OneShot learns the ordered steps
from that single video, then watches anyone else do it and reports exactly what went wrong — a skipped step, the
wrong order, the wrong place — and the moment it happened, backed by tracked visual evidence.

For training new staff, lab protocols, kitting and packing QA, assembly checks. The same learned procedure is
precise enough to hand to a (simulated) robot arm, which carries it out and checks its own work.

> Status: working Meta-model application. The judge-facing page leads with real phone recordings,
> reports abstentions explicitly, and offers live uploads with either demo reference or your own.

## What order checking adds (Nov 3)

Three checks read the same saved video-model readings, with no new model calls
(`python -m web.baseline`, full results in `web/static/media/baseline_comparison.json`).

| Check | Generated: order-only mistakes accepted (of 3) | Generated: all mistakes accepted (of 12) | Generated: sent to review (of 24) | Phone: mistakes accepted (of 6) | Phone: correct rejected (of 2) | Phone: sent to review (of 9) |
|---|---:|---:|---:|---:|---:|---:|
| Final picture only | **3** | 3 | 0 | 0 | 0 | 0 |
| Rules on the video reading | **0** | 0 | 3 | 0 | 0 | 0 |
| Rules plus tracking (shipped) | **0** | 0 | 3 | 0 | 0 | 3 |

- **What this shows:** a final-picture check accepts order-only mistakes, because their final arrangement looks right. The rule checker rejects them.
- **What it does not show:** the phone recordings contain no order-only mistake, and there the final-picture check did just as well.
- **Tracking added caution, not corrections.** Across 36 saved tracked readings, tracking changed five verdicts. All five moved to review, and none corrected a wrong reading.
- **Scope:** the final-picture check is our own simple comparison, not another product. These are three generated order-only cases and development recordings, not a held-out benchmark.

**The tracker now checks order and movement itself.** Each object gets an arrival time from its tracked boxes alone. If the reading and the tracker disagree about a required order, the result goes to review. The tracker never fails anyone on its own. A step also goes to review if the object was visible before and after it and never moved. On the saved recordings the tracker agreed with the reading on 24 required orders, disagreed on 0, and could not tell on 6. The thresholds were chosen on these same recordings, and no saved reading has a wrong order for the tracker to catch, so catching one is shown only by constructed tests.

**Items hidden by a required step no longer block a correct result.** A card on top of a cube hides the cube, as the procedure requires. That now counts when the cube's placement was confirmed first, it stayed visible until the covering step began, and the covering object is over it when it disappears. Otherwise it still requires review.

**Reported problems are double-checked before they reach a person.** Each wrong placement or skip gets two neutral multiple-choice questions about a short clip and a wider clip, with no hint of the first reading. If both show the problem, it stays a failure. Anything else goes to review, and a closer look can never turn a problem into a pass. If a look cannot run, the problem is reported as it was. Across 13 reported problems, 11 of 12 true problems were confirmed and the one false problem went to review. The first pass confirmed only 7, because a hand covers the lens as a phone recording stops. The question was changed on these same clips, so this is development evidence, and one false problem is too few to measure how often they are caught.

**The tracker now searches for procedure objects the reading left out.** A video model can omit something in plain view. If the search finds the object away from its place, the step is a clear skip. If it finds nothing, the result goes to review. If the reading misses a step but the tracker shows the object in place, that also goes to review, so a skip now takes both witnesses. This applies to the web application, not yet to the command-line tools.

**Order is now checked at the moment of placement.** A required earlier step must still hold when the later step happens. Taking the liner out, adding the charger and putting the liner back ends in the same picture, but now fails. This follows a false accept found in review. It is covered by constructed tests and changed none of 118 saved verdicts. None of those saved readings reports an object being taken back out, so behaviour on a real recording of a removal is untested. This rule depends on the video model reporting the removal. If it does not, the tracker's arrival check is the fallback, shown so far only by a constructed test.

## Dynamic object discovery (Nov 3)

Object names are discovered from each video, not selected from a packing whitelist. The reference now supplies appearance-only identity hints (including visible markings and object kind) to upload and camera reads. Absent objects must not be invented from the guide; unfamiliar objects retain their own identities. Unique visual descriptions can match renamed objects. A same-colour object of a different shape cannot silently substitute for a required item, and ambiguous matches remain unresolved. Reports show each discovered object, its visual description and tracking status separately from placement verification. The checked task vocabulary remains tabletop placements: on, inside and near. This is not a claim to recognize every possible object or procedure.

## Real packing tests and upload recovery (Nov 3)

Five personal phone recordings exercised correct packing, required order, an omitted charger,
wrong card placement, and a valid alternate order. They exposed real extraction and tracking
failures: coarse containment was mistaken for a wrong support, and a generic "white block"
tracking query followed the card instead of the charger. Physical tracking descriptions and
duplicate-identity checks now address those failures without weakening final-state checks.
A later run gave each clip one fresh reading on a single build, kept the first result, and matched what each recording shows: P01 passed, P02 failed for wrong order, P03 failed as skipped, P04 failed for wrong placement, and P05 passed with a note. P02's first reading also reported a false wrong placement of the card, which closer looks now send to review, and its box tracking timed out. These clips informed the fixes, so this is not independent validation (`single_build_run` in the report below).
These are development cases across multiple builds, not five automatic passes or an independent
accuracy benchmark. Sanitized observations, clip hashes and the two post-fix assessments are in
[`packing_development.json`](web/static/media/packing_development.json); personal videos remain private.

Uploads now show byte progress and protect against leaving mid-upload. Every follow-up updates
its result URL; reopening a completed check restores its own confirmed procedure. A private
recent-check list helps recover accepted jobs, including after refresh. Downloaded JSON reports
preserve machine assessments and separate human-review history. Additional model-proposed final
requirements are explicitly shown for keeping or removing before confirmation, rather than
remaining hidden and blocking the save. Interrupted transfers still require a new upload;
accepted jobs are recoverable, while an interrupted server-side computation is reported as stopped.

Useful routes: `GET /api/jobs` and `GET /api/jobs/{id}/report`. Both require the owning browser cookie;
recordings still expire after 24 hours. Download a report before expiry to retain it locally.

## Reusable procedures and the independent packing trial (Oct 31)

Open `/judge` for the guided three-minute demonstration, `/?teach=1#try` to teach a
reference, and `/pilot` for the prospective everyday-packing trial. Teach a reference
without requiring an attempt, confirm or correct its steps, then reuse it for uploads
and camera checks. Saved references can be reopened for editing; existing attempts and
frozen trials retain their own copies of the earlier procedure.

The packing protocol declares nine cases before inference: correct, order-only error,
skipped card, wrong accessory placement, correct placement undone, occlusion, another
camera angle, a corrected mistake, and ambiguous identical parts. The setup checks four
explicit role assignments, final requirements and liner-first rules. A plan hash freezes
the procedure, labels and source fingerprint. Changes to the source refuse new trial
submissions. Labels never enter model extraction prompts. First submissions, retakes,
clip hashes, failures, reviews, latency and optional self-timed human review measurements
stay in an owner-private downloadable report. Reused clips are rejected.

**Independent packing results and measured workplace benefits remain pending.** A second
person must supply new recordings. Anonymous participant codes and review timings are
self-reported; the optional time differences exclude model waiting and are not a
controlled impact study. The recording guide is in `../Real-footage/packing-pilot/`.

All **209 offline tests pass on Windows/Python 3.14.5 and the clean Modal Linux/Python
3.12.10 deployment image**, including the two false-accept regressions, reference-only
confirmation, frozen labels, first-submission preservation, source drift, duplicate
recordings, private reports and separate timing denominators. `pip check` passes in the
Linux image. Reproduce the image validation with `python -m modal run deploy/validate.py`.

Useful routes: `POST /api/procedures`, `GET /api/procedures`,
`POST /api/procedures/{id}/edit`, `POST /api/pilots`,
`POST /api/pilots/{id}/cases/{case}`, and `GET /api/pilots/{id}/export`.

## Verified live procedures (Oct 30 follow-up)

The live application now combines an editable procedure, continuous camera/replay monitoring,
tracking-backed completion, follow-up recordings, separate human review, and a saved decision history.
Production paths call Meta's Muse Spark and SAM services; the offline tests mock those services explicitly.

Custom references pause for confirmation before an attempt is judged. Review the full step list,
correct solid-object/container interpretations, add or remove steps, set ordering rules, and mark
required final placements. The confirmed procedure is retained with the result. A missing reference
step cannot be recovered simply by checking the attempt against the same incomplete model output.

Acceptance now checks terminal placements separately from intermediate milestones. Reaching a tray
and subsequently moving elsewhere cannot remain a verified pass. Identical object descriptions are
reported as ambiguous instead of sharing one trajectory; independent identities must be established
before those objects can corroborate one another. Missing evidence requests review.

Camera sessions receive server-sent updates and adapt their read interval to observed provider latency.
Their final assessment uses the same tracking gate as uploaded attempts. This is continuous input and
feedback, with model/provider processing delay; it is not a claim of subsecond intervention.

Recordings and derived files are outside the static directory and are accessible only to the owning
browser cookie. They expire after 24 hours by default (`RETENTION_SECONDS`) and can be deleted after
processing. Human decisions are labelled separately and never turn an unverified machine result into
machine corroboration. A clearer follow-up is assessed as a new recording against the same confirmed
procedure, with a link to the original decision; recordings are not silently fused.

The current cached phone replay resolves **6/9** expected outcomes with **3 review cases**, **0 false
accepts**, and **0 false rejects**. A previously passing take now requires review because its final
placement is not supported. The earlier report remains in `web/static/media/real_results_baseline.json`.
The same sixteen generated fixtures still match **16/16** expected outcomes, with two abstentions, in
`current_validation.json`. This is an offline development replay, not new inference or new examples.
Historical live-upload/repair reports below describe the earlier build and are not current-build evidence.
The new local recorded-camera transport check completed in **50.07 seconds** with two actual Meta
reads, zero read/tracking errors, a tracked final pass, and a private overlay that returns 404 without
the owner cookie (`web/static/media/live_session_validation.json`). This tests one existing phone
clip, not a live person at a webcam or a new accuracy benchmark. That earlier build passed 83 tests in the original and a fresh isolated Python 3.14.5 environment; the current suite and Linux validation are reported above.

```bash
python -m unittest discover -s tests -v
python -m web.build_judging --reports-only
python -m uvicorn web.server:app --port 8000
```

`requirements.lock` records the exact tested dependency versions. CI and the deployment image specify
Python 3.12. The current lock has also passed a clean Linux/Python 3.12 image install, `pip check`, and the full offline suite. Model-service and browser checks remain separate from offline tests.

Uploads, camera duration, chunk sizes, session admission, read count, private storage, and ffmpeg/ffprobe
runtime are bounded on the server. The spend ledger reserves estimated cost before concurrent model
work, preserves charges for failed/unknown work, accounts for interrupted calls on restart, and refuses
to reset a corrupted ledger. Tracking charges remain estimates because the service reports no usage;
provider bills remain authoritative. Run a single server process/container for the in-memory scheduler.

Private recordings are sent to the configured model services for processing. The browser cookie is a
session capability, not a multi-user account system. Keep the site on HTTPS for remote use. A bookmark
to `?job=<id>` resumes a pending confirmation or retrieves a retained result from the same browser;
an interrupted inference is reported as interrupted rather than silently accepted.
Retention across replacement containers requires a persistent `PRIVATE_DATA_DIR` and budget file.
Modal uses `/data/private` on its existing volume; Docker should mount a named volume at `/data`.

Useful routes: `GET /api/watch/{id}/events`, `POST /api/jobs/{id}/procedure`,
`POST /api/jobs/{id}/followup`, `POST /api/jobs/{id}/review`, `GET /api/jobs/{id}/history`,
`DELETE /api/jobs/{id}`, and the authenticated recording route `/api/assets/{id}/{filename}`.

## Evidence and reliability (Oct 30)

The phone-footage development set contains **one reference and nine separate attempts** of a two-block
stacking task. With the current conservative checker: **6/9 expected outcomes resolved, 3 abstentions,
0 false accepts among six incorrect attempts, and 0 false rejects among three acceptable/occluded attempts**.
The reference self-check is excluded from those counts; its tracker conflict now correctly requires review.
The two missing-block attempts require review because the absent block has no supporting track. A correct take also requires review because its final placement is not corroborated. The earlier 7/9 report is historical.

These are development results, not a held-out real-world benchmark: earlier checker changes were informed
by these recordings. The two reversed stacks change the final structure and are diagnosed as wrong placement;
they do not establish real-footage detection of a pure order-only error. Model naming calls the red block pink;
guided extraction reuses that identifier. The page shows every attempt and provides the full results as JSON.

The existing generated packing set still matches **24/24 expected outcomes through each of two providers**
when cached extraction outputs are replayed with the current checker. Both providers saw the same clips;
this is 24 examples evaluated twice. Hidden cases permit abstention rather than a false failure.

An earlier generated batch used seed 73129 with labels and the checker-rule fingerprint fixed before inference:
**14/16 expected outcomes matched; 2 tracking timeouts are retained as pipeline errors**. Network deadlines
were added after a request stalled; successful fresh extraction outputs were reused without changing the
checker rules. These are new variations from the same renderer, not new real tasks or a flawless benchmark.
The frozen plan and every result are downloadable from the demo (`web/static/media/judging_eval.json`).

**Tracking repair:** the same sixteen cases now match **16/16 expected outcomes, with zero pipeline or
object-tracking errors**. Fourteen extraction caches were replayed; the two former timeout cases received
new extraction and tracking, and correctly abstained because of occlusion. Their tracking stages completed
in approximately 56–73 seconds. This is a development repair check, not sixteen new examples; both the original
failure report and the repair report (`tracking_validation.json`) remain available.

The tracking path now uses a box-only response format. The original format decoded every dense pixel mask
and created cumulative snapshots, although the checker only consumes boxes. Two captured real-video responses
produced exactly the same 325 boxes with the new parser; local parsing fell from about 6/17 seconds to under
0.03 seconds each. This measures parser CPU work, not end-to-end model latency. The official stream parser
still validates response finalization; truncated responses cannot become completed evidence.

The public deployment passed **4/4 fresh upload checks**: two runs of a correct phone clip, one real wrong
placement, and one generated occlusion. They completed in **35–70 seconds**, with zero object-tracking errors
and working tracking-overlay videos. The correct runs passed, the wrong placement failed, and the occluded
attempt required review. These are four service runs over three provided examples, not an accuracy benchmark.
The timings and phase histories are packaged in `web/static/media/live_validation.json`.

Verdicts and evidence strength are separate:

- **Model only:** video interpretation without independent tracking corroboration; clearly labelled unverified.
- **Partial:** objects were tracked, but not every relation is corroborated.
- **Corroborated:** boxes support every reached placement, or show an item hidden by a required later step. This is a 2D geometric heuristic, not proof of 3D contact.
- **Inconclusive:** missing or conflicting evidence requires review. An empty reference never passes.

Tracker disagreement and unseen reported steps cannot produce a passing verdict. A final picture without
timestamps cannot establish a required sequence. Model output is validated for unique object/step identifiers,
declared references and finite increasing timestamps. Human rule changes still re-check without model calls.
Tracking and colour fallback share one 120-second deadline (`TRACK_TIMEOUT_SECONDS`). Each object request has
a 45-second deadline and one bounded retry for transient transport or incomplete-response failures. Completed
objects survive another object's failure, and failed requests are distinct from a completed no-detection result.
Missing required tracking evidence ends as **review required** rather than a verified pass.
Extraction and narration requests use a 90-second timeout (`MODEL_TIMEOUT_SECONDS`) without hidden SDK retries.

```bash
python -m unittest discover -s tests -v     # offline reliability and packaged-evidence regression checks
python -m web.build_judging                # refresh real reports + missing demo encodes; no model calls
python -m web.build_judging --reports-only # refresh packaged reports from cached/exported graphs
python probes/judging_eval.py              # fresh generated clips, frozen labels/seed/checker; calls models
python probes/tracking_eval.py             # repeat repair verification; calls models for the two former failures
python probes/tracking_eval.py --reuse-tracks # replay completed repair tracks without model calls
python probes/live_smoke.py --base-url URL # real uploads, tracking, verdicts and rendered videos
```

Raw personal recordings remain outside the repository. The demo contains silent, web-encoded copies and
sanitized graphs. Near-live latency remains approximately ten seconds on the measured generated examples;
it is not instant intervention. See `JUDGING.md` for a three-minute walkthrough and the evidence boundaries.

## What was built during the event (Oct 26 – Nov 4, 2026)

Everything in this repository was written during the event, except the third-party material listed below.

| Date | Work |
|---|---|
| Oct 27 | Repository created. Day-1 API probes: SAM 3.1 video tracking, Muse Spark 1.3 video input (three input paths), Muse Spark Open on Baseten with `video_url`. Synthetic test clip generator. |
| Oct 27 | **P0 — `oneshot/` task-graph module.** Video in → Muse Spark reads the ordered task (objects, steps, final relations) → SAM 3.1 tracks each object → every step linked to the trajectory that backs it. Evidence only ever comes from the tracker, never the language model. |
| Oct 27 | **P1 — `oneshot/sim/` executor.** The task graph runs on a simulated Franka Panda: deterministic `pick`/`place`/`stack`/`push`, geometric checking of the final relations, generalisation to shuffled layouts, recovery from a mid-run disturbance (monitor the relations, re-do the step that broke), and an honest "cannot complete" when the arm can't reach. |
| Oct 27 | **P2 — temporal monitor (`oneshot/sim/monitor.py`).** The demonstration's step order becomes a confirmed ordering rule; the monitor replays the execution trace against it. Two runs can reach the *same* final picture yet get different verdicts — only one followed the procedure. |
| Oct 27 | **Evaluation harness (`oneshot/sim/evaluate.py`).** Seeded, randomized trials over the executor + monitor — shuffle generalisation, disturbance recovery, procedure-violation detection, honest abstention — reported with full denominators. |
| Oct 27 | **Generalisation — second task.** A sorting demo (two bins) added to the clip generator and run through the *same* P0→P2 pipeline with no code changes: P0 extracted it correctly (distinguishing left/right trays), the arm executes and verifies it, and the evaluation harness measures it. |
| Oct 27 | **P2 productization — confirmable procedure.** Ordering rules are *proposed* from the demonstration to an editable spec; the user confirms/edits/adds before the monitor enforces only the confirmed rules (with validation of unknown objects). |
| Oct 28 | **Real footage.** The same pipeline run on a genuine stock clip of a child stacking coloured cubes: every block tracked, the noisy extracted plan sanitised to its executable core (target-less steps and unestablished goals dropped, transparently), and the arm reaches the goal. Proof it handles real video, not only synthetic clips. |
| Oct 28 | **P3 — open-weights path.** The task-graph extractor can run on **Muse Spark Open** (open weights) via Fireworks, selected with `--provider fireworks`: keyframes in, same JSON task graph out. Makes the open-weight route explicit for the bonus; SAM tracking stays on the hosted API. |
| Oct 28 | **Web app (live product).** A FastAPI page — pick a demonstration, see the task graph it learned, and run it on the arm live: normally, out of order (procedure check), or with a mid-run shove. The simulation runs server-side and the rendered video + verdict come back in the page. |
| Oct 28 | **Procedure checker.** `oneshot/check.py`: learn a procedure from one reference video, then check any attempt video for skipped steps, wrong order and wrong placements, each with a timestamp. Attempts are read guided by the reference's object names. 20/20 generated attempts judged correctly. The web page now leads with it. |
| Oct 28 | **Live checking.** Upload an attempt (and optionally your own reference) on the page; the server runs the real pipeline as a background job and the page shows the verdict as soon as it exists, then the tracking view. |
| Oct 28 | **Narrated references (`oneshot/narration.py`).** The reference's audio goes through **Muse Voice Transcribe** (timed lines); Muse Spark ties each line to a step — the reason given, whether it is critical, spoken order rules ("liner first", "any order"). Quotes are checked against the transcript, so a reason can't be invented. The spoken words also name the objects ("blue liner", not "blue block"). |
| Oct 28 | **Which order matters (`oneshot/procedure.py`).** Every pair of steps gets a verdict: *required* (said, or physical — the product sits on the liner), *free* (said, or a second correct reference did it the other way), or *as shown* (a different order is a note, never a failure). A person can change any rule on the page and every attempt is re-checked instantly (`POST /api/recheck`, no model calls). |
| Oct 28 | **Evidence-checked verdicts.** With tracking, each reached step is checked against SAM's boxes (*tracker agrees / seen / not seen / disagrees*), and a step whose object was hidden after the previous step is reported as **"couldn't verify"** instead of "skipped". Every problem carries its severity, the spoken reason, and a one-line fix. |
| Oct 29 | **Near-live warnings (`oneshot/watch.py`, `web/watch.py`).** While an attempt is still going on — from the browser camera, or a video replayed in real time — the video so far is re-read every 2 s and a warning appears as soon as a mistake can no longer be undone (a step done while a required earlier one isn't, or something put in the wrong place), with the spoken reason and the fix. Skips wait for the end, when the full check gives the verdict. Measured on the six sample attempts: warnings a median 10.5 s after the mistake, no false alarms, 6/6 final verdicts right. |
| Oct 29 | **Second open-weights host.** `--provider baseten` runs the extractor on Muse Spark Open via Baseten, sending the whole clip as video (Fireworks gets keyframes). Same task graph as the hosted model on the synthetic clip, and **24/24 on the pack-out checker eval** (same clips as the hosted model's 24/24; `probes/pack_eval.py --provider`). The eval exposed a checker fragility, now fixed: when a hand covered the box, whether the verdict was "can't verify" or a false "wrong place" depended on a half-second difference in the model's step end time. An object must now be seen 0.5 s after its move ends for its resting place to count as observed. |
| Oct 28 | **Pack-out demo + evaluation.** `probes/make_pack_clip.py` renders a narrated packing reference (computer voice), a second correct reference and eight kinds of attempt; `probes/pack_eval.py` measures the checker on freshly randomized attempts with expected verdicts written first. The web page now tells this story. |

## Pre-existing and third-party material

- `probes/p1_sam_video.py` follows the request shape and stream handling of Meta's
  [meta-model-cookbook](https://github.com/meta-models/meta-model-cookbook) recipe `07_sam/01_api_basics` (MIT).
- `oneshot/sim/` adapts the cookbook's `03_use_cases/14_muse_robotics` recipe (MIT): its damped-least-squares IK
  loop and the smoothstep pick-and-place phase sequence. The Franka Panda model is MuJoCo Menagerie (Apache-2.0),
  pulled at runtime by `robot_descriptions` (no model files are vendored here). That recipe turns a *spoken command*
  into robot motion; OneShot's contribution is running a *learned* task graph from a demonstration video, the
  `stack`/`push` primitives the recipe lacks, geometric relation checking, shuffle generalisation, and honest
  per-step outcomes.
- Dependencies: `openai`, `meta-sam-parser` (SAM License), `python-dotenv`, `pydantic`, `mujoco`, `robot_descriptions`.
- The real-footage demo uses a stock video by cottonbro studio from Pexels (Pexels License, free use):
  <https://www.pexels.com/video/a-kid-playing-with-wooden-building-blocks-3676968/>. The web page includes a
  downscaled copy and a version with tracking boxes drawn on it, credited here.
- Built with help from AI coding assistants; all design and code decisions are reviewed by the author.

## Setup

Requires Python 3.12+ and `ffmpeg`/`ffprobe` on PATH.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; use source .venv/bin/activate elsewhere
pip install -r requirements.txt
copy .env.example .env          # then fill in your keys
```

## Check an attempt against a reference

```bash
python -m oneshot.check REFERENCE.mp4 ATTEMPT.mp4 [ATTEMPT2.mp4 ...] [--also SECOND_REFERENCE.mp4]
```

The reference is read into ordered *milestones* — the result of each step ("the liner is in the box"). If the
reference has speech, it is transcribed first and read against the steps: reasons, critical steps and spoken
order rules. A second correct reference (`--also`) shows which orders are free. Together they give the
**procedure** (`--procedure-out` writes it as JSON): each pair of steps is *required*, *free* or *as shown*.

Each attempt is read the same way, **guided to reuse the reference's object names**, so the two line up; every
milestone is then located in time in the attempt. The report lists:

- **skipped** — a milestone never happens (with when it was expected),
- **wrong place** — the right object went somewhere else,
- **wrong order** — a required order broken (temporal monitor on the attempt's timeline); a merely different
  order from the reference is a *note*,
- **couldn't verify** — the step looks missing but its object was hidden from the tracker after the previous step,
- **check this step** — the tracker's boxes disagree with what the video model reported,
- **extra** — something the reference never did (a note).

Verdicts: *pass*, *pass with notes*, *can't verify*, *fail*. Problems carry the spoken reason, a critical flag and
a fix.

Measured with `python probes/pack_eval.py` on freshly randomized packing attempts (expected verdicts written
before running): **24/24** — 3 of 3 for each of correct, free order, unusual order (noted, not failed), required
order broken, skipped liner, skipped card, wrong place, and hidden step (reported as *can't verify*, never failed).
Extraction cost $0.24 for all 24. One hidden-step check was tightened after a run where the tracker mistook the
hand for the card (an object can't be bigger than the container it is in), so that row is not blind. The older three-tray test (`python probes/proc_eval.py`, 20 attempts) scored
20/20. These are clean generated clips with computer-voiced narration. The separate phone-footage results and
their narrower scope are reported in the evidence section above.

## Web app

The main page tells the procedure-checker story with a packing task: the narrated **reference** video (sound
toggle, *AI vision* toggle showing the tracker's box on every object, learned steps with critical flags and the
words that were said), then **which order matters** — every rule with where it came from, switchable between
*Must / Note / Free*, re-checking all attempts instantly — then eight **attempts**, each with its verdict,
problems, reasons and fixes; click a problem to jump to that moment in the video. Then the measured accuracy, and
the robot carrying out an earlier learned procedure.
A second page (`/static/robot.html`) shows the robot under stress: objects moved, a block knocked over, steps
done out of order.

```bash
python -m web.build_check                         # checker demo: videos, evidence overlays, reports
python -m web.build_media                         # robot stress-test page: videos + results
python -m uvicorn web.server:app --port 8000      # then open http://localhost:8000
```

**Live checking:** the page's *Try it with your own video* section uploads an attempt (and optionally your own
reference, plus a second correct one) to `POST /api/check`; the server runs the real pipeline — listens to the
reference, reads the steps, learns the procedure, compares, then tracks the objects and re-checks against the
tracker — and the page polls `GET /api/jobs/{id}`, showing the verdict as soon as it exists (~20 s) and the
evidence assessment and AI-vision view when tracking finishes (provider latency varies; timeouts require review). Rule changes go to
`POST /api/recheck`. One check runs at a time; uploads are limited to 2 minutes / 80 MB
since every check calls the hosted models. Without uploads, the demo sections are plain files and can be hosted
statically.

**Catch it while it happens (near-live):** the page's section 4 plays a sample attempt as if live, or records from
the browser camera (`MediaRecorder`, 1 s WebM chunks to `POST /api/watch/{id}/chunk`). At an interval adapted to observed provider delay the page asks for a
read (`POST /api/watch/{id}/read`): the server cuts the video so far into a small 5 fps clip and Muse Spark reads it
at *minimal* reasoning effort (~8 s, ~$0.007; the model samples frames itself, so the low frame rate only shrinks
the upload). Up to four reads overlap, and a slow read never overwrites a longer one. Each read is judged only for what is
already certain (`oneshot.watch.wrong_now`): a step done while a required earlier step isn't (grouped on the
missing step, so one root cause is one warning), a required order already broken, or an object put somewhere the
procedure never puts it. Steps that ended within 0.5 s of the cut are ignored (an object in the hand isn't placed).
Warnings persist across reads: a wrong place becomes *fixed* once the object reaches its right place, and one that
two later reads in a row no longer see is *withdrawn*, shown struck through rather than silently dropped. When the
attempt ends (`POST /api/watch/{id}/finish`) the whole video gets the usual full check at normal effort; that is the
verdict, and it is where skipped steps appear. Two sessions at a time, at most 60 reads each.

```bash
python -m oneshot.watch web/reference.graph.json ATTEMPT.mp4 --procedure web/reference.procedure.json   # CLI replay
python probes/watch_latency.py          # with the web app running: delay, false alarms, final verdicts
```

`probes/watch_latency.py` on the six sample attempts (2 s reads): 6/6 final verdicts right, 0 false alarms on the
two passing attempts, failures warned a median **10.5 s** (max 11.9 s) after they happened, $0.02–0.04 per attempt.

## Day-1 probes

```bash
python probes/make_test_clip.py                                   # clips/synthetic.mp4 (stack task), no camera needed
python probes/make_test_clip.py --task sort                       # clips/sort.mp4 (sorting task)
python probes/p1_sam_video.py clips/synthetic.mp4 --concept "red square" --concept "blue square"
python probes/p2_spark_video.py clips/synthetic.mp4               # paths A, B, C
python probes/p3_spark_open_video.py clips/synthetic.mp4          # needs BASETEN_API_KEY
```

Each probe prints a summary and writes a JSON report (latency, cost estimate, raw output) to `probes/out/`.

## Build a task graph (P0)

```bash
python -m oneshot clips/synthetic.mp4            # writes runs/<clip>.graph.json
```

This is the first stage of the pipeline and the input contract for the executor. It
runs Muse Spark 1.3 to read the task (objects, ordered `pick`/`place`/`stack`/`push`
steps with timestamps, and the relations that hold at the end), then SAM 3.1 to track
each object across the clip, and links every step to the tracked boxes inside its time
window. Objects SAM cannot track are reported as warnings, not dropped silently. The
JSON holds the full per-object trajectory; the console prints a short digest.

### Open-weights extractor (P3)

By default the task graph is read by the hosted Muse Spark 1.3 (native video). The same
step can run on **Muse Spark Open** — an open-weight model — on either of two inference hosts,
which makes the open-weight route explicit (the bonus path). SAM tracking is unchanged.

| Provider | Model | Input |
|---|---|---|
| `--provider baseten` | `meta-models/muse-spark-open-1026` | the whole clip (`video_url`) |
| `--provider fireworks` | `accounts/fireworks/models/muse-spark-open` | 12 evenly spaced keyframes |

```bash
# in .env: BASETEN_API_KEY (model defaults to meta-models/muse-spark-open-1026)
python probes/p3_spark_open_video.py clips/synthetic.mp4      # checks the key, slug and video input
python -m oneshot clips/synthetic.mp4 --provider baseten

# in .env: FIREWORKS_API_KEY and FIREWORKS_MODEL (the Spark Open slug)
python probes/p4_fireworks_models.py                          # find the Spark Open slug on your account
python -m oneshot clips/synthetic.mp4 --provider fireworks
```

Verified on the synthetic clip: Muse Spark Open on **both** hosts produced the **same task
graph** as the hosted model — identical steps and final relations — and it runs through
execution and the temporal monitor unchanged (SUCCESS, procedure followed).

On the procedure checker, Muse Spark Open on Baseten judges **24/24** randomized pack-out
attempts correctly — the same score as the hosted model on the same clips:

```bash
python probes/pack_eval.py --provider baseten    # graphs cached as runs/pack_eval/<stem>.baseten.graph.json
```

## Deploy (live product)

The page runs as one CPU container with `ffmpeg`. The scheduler and active sessions are in memory; retained job/procedure metadata, recordings and the budget ledger use the mounted data volume. Keep a single instance. Every model call the public page triggers is charged to a spend ledger (`web/budget.py`): once
`SPEND_CAP_USD` (default 15) is reached, new checks are refused with a clear message while the precomputed sample
verdicts keep working. `MAX_UPLOAD_SECONDS` (default 120) bounds each upload; checks queue one at a time, at most
three waiting; near-live sessions are capped at two at once and 60 reads each. `GET /api/health` reports the spend.

```bash
pip install modal && modal setup                        # once: sign in
modal secret create oneshot-keys --from-dotenv .env     # once: the model keys (never commit .env)
modal deploy deploy/modal_app.py                        # prints the public URL
```

`Dockerfile` builds the same app for any other container host (`docker build -t oneshot . && docker run -v oneshot-data:/data -p 8000:8000
--env-file .env oneshot`).

## Run on the simulated arm (P1)

```bash
python -m oneshot.sim runs/synthetic.graph.json          # executes the graph, prints an outcome report
python -m oneshot.sim runs/synthetic.graph.json --nudge red_block 0.0 -0.22 2   # shove red after step 2, then recover
python -m oneshot.sim runs/synthetic.graph.json --reorder                       # run the steps out of order; the monitor flags it
python -m oneshot.sim runs/synthetic.graph.json --video runs/run.mp4            # also record the run (needs imageio)
```

The executor builds a MuJoCo tabletop scene from the graph's objects (manipulable things
become blocks; trays/bins become target regions), stands a Franka Panda in front of it,
and runs each step with a deterministic skill. After the steps a monitor re-checks every
relation the graph said should hold and, for any that doesn't, re-does the step that
establishes it from the current scene — so a `--nudge` that knocks an object out of place
mid-run is detected and repaired. It reports `SUCCESS`, `WRONG FINAL STATE`, or
`INCOMPLETE` with per-step residuals, repair rounds, and full denominators. The same plan
runs against a shuffled starting layout, and the arm stops and says so — without moving an
object it never reached — when a waypoint is out of reach.

**Did it follow the procedure?** The final picture doesn't tell you *how* a task was done.
The demonstration establishes its relations in an order (place the base, then stack on it),
and `oneshot/sim/monitor.py` reads that order off the task graph as a confirmed ordering
rule, then replays the execution trace against it. `--reorder` runs the same steps
backwards: the disturbance-recovery still repairs the end state, so it reports `SUCCESS`
with every final relation holding — but the temporal monitor reports `procedure: VIOLATED`,
because the stack was established before the base was in place. Same final picture, only one
run followed the procedure.

The ordering rules are *proposed* from the demonstration, then **confirmed or edited by the
user** before the monitor enforces them — the demonstration suggests the procedure, the user
owns it:

```bash
python -m oneshot.sim runs/sort.graph.json --propose-procedure runs/sort.procedure.json
# edit runs/sort.procedure.json: set confirmed:true on rules you accept; edit/add/delete freely
python -m oneshot.sim runs/sort.graph.json --procedure runs/sort.procedure.json
```

Proposed rules start `confirmed: false`, so an unedited spec enforces nothing. Only confirmed
rules are checked; editing a rule (even reversing it) changes the verdict, and a confirmed
rule that names an object not in the task is flagged.

### On real footage

A graph extracted from a clean, designed clip executes as-is. A graph read from an
unconstrained real video is noisier — a step may lack a target, or the model may assert a
final relation no step builds. Before running, the executor **sanitises the plan** to its
self-consistent core: it infers a missing target from a final relation about the same object
or drops that step, and keeps only goals that some step actually establishes. The raw
extraction is left untouched; every drop or inference is printed as a `[plan]` note, so the
run stays honest about what it chose to execute.

## Evaluation (seeded, reproducible)

```bash
python -m oneshot.sim.evaluate runs/synthetic.graph.json --trials 25 --seed 0   # stack task
python -m oneshot.sim.evaluate runs/sort.graph.json      --trials 25 --seed 0   # sort task
```

The harness fixes a task graph and varies the world — random layouts, random (in-reach)
shoves, reversed execution, out-of-reach targets — so every number has a full denominator
and is reproducible from its seed. Run on **two different demonstration videos** to show
the same code generalises (25 trials per category, seed 0):

| Axis | Stack | Sort | What it measures |
|---|---|---|---|
| shuffle | 25/25 | 24/25 | reaches the goal from a randomized starting layout |
| procedure | 25/25 | 21/24 | of the successful runs, followed the demonstrated order |
| recovery | 25/25 | 19/25 | reaches the goal despite an in-reach mid-run shove |
| detection | 25/25 | 23/25 | an out-of-order run is flagged by the temporal monitor |
| abstention | 25/25 | 25/25 | never reports success when the target is out of reach |

The numbers are kept honest: the two-bin **sort** task is harder for the push skill, so its
rates are lower, and nothing counts as success unless the geometric check and the monitor
agree. The stack graph is `runs/synthetic.graph.json`; the sort graph
(`runs/sort.graph.json`) was extracted by the *same* P0 pipeline from a second clip, with
no code changes — `python probes/make_test_clip.py --task sort` then `python -m oneshot
clips/sort.mp4`.

## Limitations (kept honest from day 1)

- Simulation first. Grasping is a kinematic attach, not a friction grasp; nothing here is validated on a physical robot.
- `push` is modelled as a no-lift surface slide (distinct from `pick`), not full non-prehensile contact dynamics.
- Results are reported with full denominators (successes, failures, abstentions, human corrections).
- Near-live means about ten seconds behind, not instant: each read of the video so far takes ~8 s. Its numbers
  come from generated clips; the camera path was tested by streaming a recorded WebM in pieces, not yet with a
  person working in front of a camera.
