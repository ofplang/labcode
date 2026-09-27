"""Run every LADS OPC UA integration example against a running reference lab, and report.

The LADS counterpart of `run_all_sila2_examples.py`. There are no LADS workflows of their own:
which protocol reaches a machine is a property of the lab, so each example is a SiLA2 example's
workflow, boundary and checks run with a `flavor: lads` environment instead -- `lads_seal.env.yaml`
for `sila2_seal`, and `lads_plate_cycle.env.yaml` for `sila2_plate_cycle`. A pass therefore says
that the same workflow reaches the same outcome through either protocol.

Prerequisites:

  * the lab is up with its LADS servers (`docker compose --profile lads up -d` in
    ofplang-mocklab), on its default ports -- the ones the environments name;
  * `asyncua` is importable by this interpreter (`uv sync --extra lads`), since labcode runs
    each script in a child launched with `sys.executable`;
  * the world is at t=0 -- one plate on `station.slot1`. Both examples are round trips that
    put the plate back and leave every instrument as they found it, so they can follow one
    another (and the SiLA2 examples) without intervention; a run that failed part way may not
    have, and restoring the world is the operator's job (`curl -X POST
    http://localhost:8001/reseed`).

Each example is run in this process, one after another: a failing one is reported and the rest
still run, so a single pass says which of them work.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

import run_sila2_plate_cycle
import run_sila2_seal

HERE = Path(__file__).parent

#: (label, module, argv) per example. The module owns the checks for its workflow; the argv
#: points it at the LADS environment.
EXAMPLES: tuple[tuple[str, Any, list[str]], ...] = (
    ("lads_seal", run_sila2_seal, ["--env", str(HERE / "lads_seal.env.yaml")]),
    (
        "lads_plate_cycle",
        run_sila2_plate_cycle,
        ["--env", str(HERE / "lads_plate_cycle.env.yaml")],
    ),
)

#: Every example takes the same tick length, and they agree on what it should be.
DEFAULT_SECONDS_PER_TICK = run_sila2_seal.SECONDS_PER_TICK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seconds-per-tick",
        type=float,
        default=DEFAULT_SECONDS_PER_TICK,
        help="real seconds per environment tick, passed to every example"
        f" (default {DEFAULT_SECONDS_PER_TICK:g})",
    )
    parser.add_argument(
        "--artifacts",
        metavar="DIR",
        help="write each example's documents under DIR/<label> (default: temporary)",
    )
    arguments = parser.parse_args(argv)

    outcomes: list[tuple[str, int]] = []
    for label, module, example_argv in EXAMPLES:
        print(f"\n=== {label} ===")
        argv_for_example = [*example_argv, "--seconds-per-tick", str(arguments.seconds_per_tick)]
        if arguments.artifacts:
            argv_for_example += ["--artifacts", str(Path(arguments.artifacts) / label)]
        try:
            code = module.main(argv_for_example)
        except Exception:  # noqa: BLE001 - one example must not stop the others
            traceback.print_exc()
            code = 1
        outcomes.append((label, code))

    print("\n=== summary ===")
    for label, code in outcomes:
        print(f"{'PASS' if code == 0 else 'FAIL'}  {label}")
    failed = [label for label, code in outcomes if code != 0]
    if failed:
        print(f"\n{len(failed)} of {len(outcomes)} examples failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
