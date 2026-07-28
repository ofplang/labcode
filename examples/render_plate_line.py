"""Run the `plate_line` example on the labcode backend and write its outputs.

This mirrors ofplang-run's `render_*.py` scripts, but drives the workflow on the
**labcode backend**: each device operation's and each move's Python is sourced from
the environment's `x-labcode.script` (see plate_line.env.yaml) and run out-of-process
on a wall clock -- exactly what `lc run --env ... --boundary ...` does.

A Plate flows down a three-station line and its optical density is read off it:

    load ──[move]──> read ──[move]──> store

`load` creates the Plate, `read` measures `od = 0.42` (the Plate is carried through
by objects.map), `store` takes it. The run boundary (plate_line.boundary.yaml) names
the whole-workflow output `od` so it is echoed in the result boundary.

Run it:

    python examples/render_plate_line.py

It writes three artifacts under examples/outputs/:
  - plate_line.plan.yaml        -- the final execution schedule (§6/§7 status doc)
  - plate_line.observation.yaml -- the observation document (D38: each completed
                                    activity's I/O views), as `lc run` would stream it
  - plate_line.svg              -- a Gantt chart of that schedule (device view)

and prints the result boundary (the produced `od`). Requires the sibling
`ofplang-schedule` (the runner replans through it, and its visualizer draws the SVG).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from ofplang.run import run_workflow
from ofplang.run.runner import load_document, serialize_document
from ofplang.schedule.scheduler.visualize import render_svg

from labcode.backend import labcode_backend_factory
from labcode.idgen import SeededUuid4Generator
from labcode.objectid import inject_boundary_ids, inject_id_field

HERE = Path(__file__).parent
OUT = HERE / "outputs"
WORKFLOW = HERE / "plate_line.workflow.yaml"
ENVIRONMENT = HERE / "plate_line.env.yaml"
BOUNDARY = HERE / "plate_line.boundary.yaml"

# A small real-seconds-per-tick keeps the out-of-process demo quick (the README uses
# 0.2 for `lc run`); the default (~20 s) suits real, slow hardware.
SECONDS_PER_TICK = 0.2


def main() -> None:
    OUT.mkdir(exist_ok=True)

    boundary = load_document(str(BOUNDARY))

    # Mirror `lc run`'s labcode Object-identity handling: inject the reserved `_id` view
    # field into every Object type, mint boundary Object ids, and share one IdGenerator
    # with the backend (which mints created Objects' ids). Runs the rewritten document in
    # memory -- run_workflow accepts a mapping (with validate=False).
    id_gen = SeededUuid4Generator()
    workflow_run = inject_id_field(load_document(str(WORKFLOW)))
    boundary = inject_boundary_ids(boundary, workflow_run, id_gen)
    factory = labcode_backend_factory(seconds_per_tick=SECONDS_PER_TICK, id_generator=id_gen)

    # Validation already happens at the `lc run` front door; here we run trusting and
    # stream the observation document straight to its output file (as `lc run` would).
    result = run_workflow(
        workflow_run,
        str(ENVIRONMENT),
        boundary,
        # Mirror `lc run`'s cadence: a running-task margin of at least the poll interval,
        # so an op that overruns its estimate (real, wall-clock timing) does not get a
        # successor dispatched onto a still-busy device.
        poll_interval=1,
        running_task_margin=1,
        random_seed=0,
        backend_factory=factory,
        validate=False,
        observation_out=str(OUT / "plate_line.observation.yaml"),
    )

    status = result.status
    # The status carries no solver objective; label the chart with its makespan (as
    # render_reroute.py does) so the visualizer draws the makespan marker.
    status.setdefault("objective", {"kind": "makespan", "value": status.get("now")})

    (OUT / "plate_line.plan.yaml").write_text(serialize_document(status), encoding="utf-8")
    (OUT / "plate_line.svg").write_text(
        render_svg(status, view="device"), encoding="utf-8"
    )
    (OUT / "plate_line.boundary.yaml").write_text(
        yaml.safe_dump(result.result_boundary, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    print(f"result boundary : {result.result_boundary}")
    print(f"makespan        : {status.get('now')}")
    print(f"wrote {OUT / 'plate_line.plan.yaml'}")
    print(f"wrote {OUT / 'plate_line.observation.yaml'}")
    print(f"wrote {OUT / 'plate_line.svg'}")
    print(f"wrote {OUT / 'plate_line.boundary.yaml'}")


if __name__ == "__main__":
    main()
