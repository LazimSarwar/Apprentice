# OneShot

Show a robot a tabletop task once, on an ordinary phone video. OneShot turns the video into an evidence-linked
task graph, runs it on a simulated arm, keeps working when the objects are shuffled, notices when something goes
wrong, and checks *how* the task was done, not just the final picture.

> Status: day 1 of the build. This README is updated as each piece lands.

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
  <https://www.pexels.com/video/a-kid-playing-with-wooden-building-blocks-3676968/>. It is demonstration *input*
  only — not redistributed in this repository.
- Built with help from AI coding assistants; all design and code decisions are reviewed by the author.

## Setup

Requires Python 3.12+ and `ffmpeg`/`ffprobe` on PATH.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; use source .venv/bin/activate elsewhere
pip install -r requirements.txt
copy .env.example .env          # then fill in your keys
```

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
step can run on **Muse Spark Open** — an open-weight model — via Fireworks, which makes the
open-weight route explicit (the bonus path). It takes evenly spaced keyframes rather than
video; SAM tracking is unchanged.

```bash
# in .env: FIREWORKS_API_KEY and FIREWORKS_MODEL (the Spark Open slug)
python probes/p4_fireworks_models.py                 # find the Spark Open slug on your account
python -m oneshot clips/synthetic.mp4 --provider fireworks
```

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
