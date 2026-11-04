# Usage and reference

Full command-line, web, deployment and simulation details. Run commands from the repository root. For the overview, see [README.md](README.md).

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
their narrower scope are reported in [CHANGELOG.md](CHANGELOG.md).

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
