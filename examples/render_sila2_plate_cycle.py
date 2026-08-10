"""Run the `sila2_plate_cycle` circuit against a real SiLA2 lab and write its outputs.

This is to `sila2_plate_cycle` what `render_plate_line.py` is to `plate_line`: it drives the
workflow through `labcode.run_labcode` -- the canonical labcode entry, exactly as `lc run --env
... --boundary ...` does -- and keeps the resulting documents under `examples/outputs/` so the
example's schedule can be read (and its Gantt chart looked at) without a lab to hand.

The difference from `render_plate_line.py` is the lab: that one's scripts are mocks that need
nothing but Python, while these open SiLA2 connections and issue real commands. The
prerequisites are therefore the integration check's:

  * the lab is up (`docker compose up -d` in ofplang-sila2-backend);
  * `sila2` is importable by this interpreter (`uv sync --extra sila2`), since labcode runs
    each script in a child launched with `sys.executable`;
  * the world is at t=0 -- one plate on `station.slot1`
    (`curl -X POST http://localhost:8001/reseed`).

This script asserts nothing: it is a producer, and `run_sila2_plate_cycle.py` is the check.
Run that first if what you want to know is whether the example still works.

Run it:

    python examples/render_sila2_plate_cycle.py

It writes four artifacts under examples/outputs/:
  - sila2_plate_cycle.plan.yaml        -- the final execution schedule (§6/§7 status doc):
                                          every activity, `completed`
  - sila2_plate_cycle.observation.yaml -- the observation document (D38: each completed
                                          activity's I/O views, incl. each Object's `_id`)
  - sila2_plate_cycle.svg              -- a Gantt chart of that schedule (device view)
  - sila2_plate_cycle.boundary.yaml    -- the result boundary (the three readings and the
                                          returned plate, as `--boundary-out` writes it)

The committed copies were produced against ofplang-sila2-backend v0.3.0 on its
`command_durations.realistic.yaml` profile, which is the one the environment's durations are
written for. Because every op runs out-of-process on a wall clock against real servers, the
exact times (and the makespan) vary between runs; the sequence of activities, the identities
and the produced values do not.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from ofplang.run.runner import load_document, serialize_document
from ofplang.schedule.scheduler.visualize import render_svg

from labcode.runner import run_labcode

HERE = Path(__file__).parent
OUT = HERE / "outputs"
WORKFLOW = HERE / "sila2_plate_cycle.workflow.yaml"
ENVIRONMENT = HERE / "sila2_plate_cycle.wrapped.env.yaml"
BOUNDARY = HERE / "sila2_plate_cycle.boundary.yaml"

# One real second per environment tick, as the integration check uses: the environment's
# durations are written in the same units as the lab's realistic timing profile, so at 1.0 the
# two agree. (`render_plate_line.py` can afford 0.2 because its scripts are mocks; here every
# tick is a real instrument waiting.)
SECONDS_PER_TICK = 1.0


def main() -> None:
    OUT.mkdir(exist_ok=True)

    boundary = load_document(str(BOUNDARY))

    # `run_labcode` is the canonical labcode entry: it injects the reserved `_id` into every
    # Object type, mints the boundary Object ids, and shares one IdGenerator with the backend
    # -- so the Plate carries one stable, reproducible identity through all four instruments in
    # the observation. Validation already happens at the `lc run` front door (and in
    # run_sila2_plate_cycle.py); here we run trusting and stream the observation document
    # straight to its output file.
    result = run_labcode(
        str(WORKFLOW),
        str(ENVIRONMENT),
        boundary,
        # Mirror `lc run`'s cadence: a running-task margin of at least the poll interval, so an
        # op that overruns its estimate does not get a successor dispatched onto a still-busy
        # instrument.
        poll_interval=1,
        running_task_margin=1,
        random_seed=0,
        seconds_per_tick=SECONDS_PER_TICK,
        observation_out=str(OUT / "sila2_plate_cycle.observation.yaml"),
    )

    status = result.status
    # The status carries no solver objective; label the chart with its makespan (as
    # render_plate_line.py does) so the visualizer draws the makespan marker.
    status.setdefault("objective", {"kind": "makespan", "value": status.get("now")})

    (OUT / "sila2_plate_cycle.plan.yaml").write_text(
        serialize_document(status), encoding="utf-8"
    )
    (OUT / "sila2_plate_cycle.svg").write_text(
        render_svg(status, view="device"), encoding="utf-8"
    )
    (OUT / "sila2_plate_cycle.boundary.yaml").write_text(
        yaml.safe_dump(result.result_boundary, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    print(f"result boundary : {result.result_boundary}")
    print(f"makespan        : {status.get('now')}")
    print(f"wrote {OUT / 'sila2_plate_cycle.plan.yaml'}")
    print(f"wrote {OUT / 'sila2_plate_cycle.observation.yaml'}")
    print(f"wrote {OUT / 'sila2_plate_cycle.svg'}")
    print(f"wrote {OUT / 'sila2_plate_cycle.boundary.yaml'}")


if __name__ == "__main__":
    main()
