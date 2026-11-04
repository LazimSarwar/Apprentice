# Changelog

Newest first. Each entry keeps the numbers and wording from that date, so older counts may differ from the current summary in [README.md](README.md).

## Final submission reliability checks (Nov 4)

Object matching now checks supplied colour, shape, kind and description together when IDs change. A copied description cannot override contradictory colour or shape. Explicitly negated markings (for example, a logo versus no logo) request identity review; missing details and compatible refinements remain allowed. Three new regression tests cover these cases.

The judge walkthrough now applies a rule change only after a successful response, blocks overlapping submissions, and ignores replies for an example the viewer has left. Five JavaScript tests exercise successful changes, restoration, rapid clicks, navigation during a request, stale responses and retry after a network failure. CI runs these alongside the Python checks.

No new recordings or accuracy measurements were added. Existing development evidence and its limitations remain unchanged.

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

Legacy references no longer reduce a specific colour-qualified object name to generic geometry when no tracking description exists (for example, a named charger stored as a block). Explicit appearance descriptions still take precedence; this changes the search query, not the criteria for accepting evidence. Missing-step reports now identify duplicate tracking as identity confusion instead of claiming an object was hidden. New regression tests cover named objects, generic IDs, preserved descriptions and omitted-object searches.

Reused object IDs now undergo appearance consistency checks. Conflicting colour, shape, kind or visual markings request identity review and cannot produce a verified pass. Missing details and compatible refinements remain allowed. The judge page shows the latest recorded P01–P05 run first, labels its evidence limitations, and preserves earlier outcomes in the downloadable report.


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
controlled impact study. The recording guide is in [`docs/packing-trial-recording-guide.md`](docs/packing-trial-recording-guide.md).

All **218 offline tests pass on Windows/Python 3.14.5 and the clean Modal Linux/Python
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
