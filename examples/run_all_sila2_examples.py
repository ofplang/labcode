"""Run every SiLA2 integration example against a running reference lab, and report.

There are two environments for the `sila2_seal` workflow -- the `flavor: sila2` one and the
`raw` one -- and both are meant to keep working. This runs each in turn and exits non-zero if
any of them failed, so one command answers "does labcode still drive a real lab".

Only the examples that need the lab are here. `render_plate_line.py` needs nothing but Python
and is not part of this check, so that a missing prerequisite cannot be confused with a
regression.

Prerequisites (the same as one example on its own):

  * the lab is up (`docker compose up -d` in ofplang-sila2-backend);
  * `sila2` is importable by this interpreter (`uv sync --extra sila2`), since labcode runs
    each script in a child launched with `sys.executable`;
  * the world is at t=0 -- one plate on `station.slot1`. Every example here is a round trip
    that puts the plate back, so they can follow one another without intervention; a run that
    failed part way may not have, and restoring the world is the operator's job
    (`curl -X POST http://localhost:8001/reseed`).

Each example is run in this process, one after another: a failing one is reported and the rest
still run, so a single pass says which of them work.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import run_sila2_seal

HERE = Path(__file__).parent

#: (label, argv) per example. The label is what the summary calls it.
EXAMPLES: tuple[tuple[str, list[str]], ...] = (
    ("sila2_seal (flavor: sila2)", ["--env", str(HERE / "sila2_seal.wrapped.env.yaml")]),
    ("sila2_seal (raw)", ["--env", str(HERE / "sila2_seal.env.yaml")]),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seconds-per-tick",
        type=float,
        default=run_sila2_seal.SECONDS_PER_TICK,
        help="real seconds per environment tick, passed to every example"
        f" (default {run_sila2_seal.SECONDS_PER_TICK:g})",
    )
    parser.add_argument(
        "--artifacts",
        metavar="DIR",
        help="write each example's documents under DIR/<label> (default: temporary)",
    )
    arguments = parser.parse_args(argv)

    outcomes: list[tuple[str, int]] = []
    for label, example_argv in EXAMPLES:
        print(f"\n=== {label} ===")
        argv_for_example = [*example_argv, "--seconds-per-tick", str(arguments.seconds_per_tick)]
        if arguments.artifacts:
            directory = Path(arguments.artifacts) / label.replace(" ", "_").replace(":", "")
            argv_for_example += ["--artifacts", str(directory)]
        try:
            code = run_sila2_seal.main(argv_for_example)
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
