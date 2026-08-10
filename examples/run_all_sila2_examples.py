"""Run every SiLA2 integration example against a running reference lab, and report.

Three checks are meant to keep working: the `sila2_seal` workflow through each of its two
environments (the `flavor: sila2` one and the `raw` one), and the `sila2_plate_cycle` circuit
through four instruments. This runs each in turn and exits non-zero if any of them failed, so
one command answers "does labcode still drive a real lab".

Only the examples that need the lab are here. `render_plate_line.py` needs nothing but Python
and is not part of this check, so that a missing prerequisite cannot be confused with a
regression.

Prerequisites (the same as one example on its own):

  * the lab is up (`docker compose up -d` in ofplang-sila2-backend);
  * `sila2` is importable by this interpreter (`uv sync --extra sila2`), since labcode runs
    each script in a child launched with `sys.executable`;
  * the world is at t=0 -- one plate on `station.slot1`. Every example here is a round trip
    that puts the plate back and leaves every instrument open, so they can follow one another
    without intervention; a run that failed part way may not have, and restoring the world is
    the operator's job (`curl -X POST http://localhost:8001/reseed`).

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

#: (label, module, argv) per example. The label is what the summary calls it; the module owns
#: the checks for its own workflow, so each entry names the one that can judge it.
EXAMPLES: tuple[tuple[str, Any, list[str]], ...] = (
    (
        "sila2_seal (flavor: sila2)",
        run_sila2_seal,
        ["--env", str(HERE / "sila2_seal.wrapped.env.yaml")],
    ),
    ("sila2_seal (raw)", run_sila2_seal, ["--env", str(HERE / "sila2_seal.env.yaml")]),
    ("sila2_plate_cycle", run_sila2_plate_cycle, []),
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
            directory = Path(arguments.artifacts) / label.replace(" ", "_").replace(":", "")
            argv_for_example += ["--artifacts", str(directory)]
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
