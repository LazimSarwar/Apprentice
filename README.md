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
python probes/make_test_clip.py                                   # clips/synthetic.mp4, no camera needed
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

## Run on the simulated arm (P1)

```bash
python -m oneshot.sim runs/synthetic.graph.json          # executes the graph, prints an outcome report
python -m oneshot.sim runs/synthetic.graph.json --nudge red_block 0.0 -0.22 2   # shove red after step 2, then recover
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

## Limitations (kept honest from day 1)

- Simulation first. Grasping is a kinematic attach, not a friction grasp; nothing here is validated on a physical robot.
- `push` is modelled as a no-lift surface slide (distinct from `pick`), not full non-prehensile contact dynamics.
- Results are reported with full denominators (successes, failures, abstentions, human corrections).
