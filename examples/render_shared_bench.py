"""Run the `shared_bench` example -- two jobs in one laboratory -- and write its outputs.

The companion of `render_plate_line.py`, driving `labcode.run_labcode` exactly as
`lc run --jobs examples/shared_bench.run.yaml --env examples/shared_bench.env.yaml`
does. Where that example runs one workflow down a line, this one runs the *same* workflow
twice at once and lets the plan sort out who gets the bench:

    rack.slot_a ─┐                      ┌─ rack.slot_a       morning
                 ├─ arm ─▶ reader.stage ┤
    rack.slot_b ─┘                      └─ rack.slot_b       afternoon

One reader and one arm, a slot per job. The two jobs are planned **together** (schedule
SPEC §6.11), so they take turns on the reader instead of one run waiting for the other
to finish -- which is what planning them together is for.

🔴 What to look at: the two jobs run the same file, so every node path and port name
they render is identical. What tells their work apart is the **job** -- each Object's
`_id` is minted per job (see the printed result boundary: two different tubes), and
`--trace` records the job on every operation. Without that a reproducible generator
would hand both tubes one identity.

Run it:

    python examples/render_shared_bench.py

It writes four artifacts under examples/outputs/:
  - shared_bench.plan.yaml        -- the final execution schedule (§6/§7), every activity
                                     carrying the `job` it belongs to
  - shared_bench.observation.yaml -- the observation document (D38), likewise per job
  - shared_bench.svg              -- a Gantt chart (device view): the reader's lane shows
                                     the two jobs queueing for it
  - shared_bench.boundary.yaml    -- the result boundary, **per job**: each tube back on
                                     its own slot, with its own `_id` and its own `od`

🔴 These are a record of one run, not a golden: labcode runs out of process on a wall
clock, so the times and the arrangement move between runs (examples/README.md). The one
artifact that does not is the result boundary, which carries no times.

Requires the sibling `ofplang-schedule` (the runner replans through it, and its
visualizer draws the SVG).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from ofplang.run.runner import load_document, parse_run_document, serialize_document
from ofplang.schedule.scheduler.visualize import render_svg

from labcode.runner import run_labcode

HERE = Path(__file__).parent
OUT = HERE / "outputs"
RUN_DOCUMENT = HERE / "shared_bench.run.yaml"
ENVIRONMENT = HERE / "shared_bench.env.yaml"

# As in render_plate_line.py: a small real-seconds-per-tick keeps the out-of-process demo
# quick. The reader takes 6 ticks and each move 2, so two jobs sharing one reader is a
# few seconds of wall clock.
SECONDS_PER_TICK = 0.2


def main() -> None:
    OUT.mkdir(exist_ok=True)

    # The run document is the joint entry: which jobs, each one's workflow and boundary.
    # Parsed the way `lc run --jobs` parses it, relative to this directory.
    run_doc = parse_run_document(
        load_document(str(RUN_DOCUMENT)), RUN_DOCUMENT.parent
    )

    # `run_labcode` takes the roster in place of a workflow. It does per job what it does
    # for a lone workflow -- inject `_id` into the Object types, mint the boundary's ids,
    # share one IdGenerator with the backend -- and keys the minting by the job, so the
    # two tubes are two tubes.
    result = run_labcode(
        list(run_doc.jobs),
        str(ENVIRONMENT),
        # No boundary for the run: a boundary belongs to a job, and each carries its own.
        poll_interval=1,
        running_task_margin=1,
        random_seed=0,
        seconds_per_tick=SECONDS_PER_TICK,
        observation_out=str(OUT / "shared_bench.observation.yaml"),
        # The default, spelled out because it is the interesting one here: if the morning
        # tube is dropped, the afternoon's still gets measured.
        on_job_failure="continue",
    )

    status = result.status
    status.setdefault("objective", {"kind": "makespan", "value": status.get("now")})

    (OUT / "shared_bench.plan.yaml").write_text(
        serialize_document(status), encoding="utf-8"
    )
    (OUT / "shared_bench.svg").write_text(
        render_svg(status, view="device"), encoding="utf-8"
    )
    (OUT / "shared_bench.boundary.yaml").write_text(
        serialize_document(result.result_boundary), encoding="utf-8"
    )

    print(f"makespan: {status.get('now')}")
    print(yaml.safe_dump(result.result_boundary, sort_keys=False).rstrip())


if __name__ == "__main__":
    main()
