"""Drive `sila2_seal` against a running lab of real SiLA2 servers, and check the outcome.

This is the integration check for labcode's SiLA2 story: unlike `render_plate_line.py`, whose
scripts are mocks that only return values, this one's environment scripts open SiLA2
connections and issue real commands. What it proves is the whole path -- labcode schedules,
dispatches out-of-process, a script talks SiLA2, an instrument acts, and the produced value
comes back through labcode's partial outputs.

VERIFIED AGAINST: ofplang-sila2-backend v0.3.0 (commit 0c3c4c8). That lab is a *reference*,
not a requirement: `sila2_seal.env.yaml` speaks plain SiLA2 and can be pointed at real
instruments by changing the host and port in it. The version is recorded so a run without
hardware has something known to reproduce against -- it is deliberately NOT asserted on,
because checking a server's name or version would be the one thing that stops this script
working against the instruments it is meant to be portable to.

WHAT IT DOES NOT DO: it never contacts the reference lab's world-state service. That service
stands in for the physical world, which exposes no such interface, so a client that read it
could not be pointed at hardware. Everything asserted below comes from what a real client can
see: labcode's own status and observation documents, the produced boundary, and readings the
instruments themselves report.

Prerequisites:

  * the lab is up (`docker compose up -d` in ofplang-sila2-backend);
  * `sila2` is installed in *this* interpreter's environment -- labcode runs each script in a
    child process launched with `sys.executable`, so the client library has to be importable
    there (`uv sync --extra sila2`, or `pip install 'labcode[sila2]'`);
  * the world is at t=0, i.e. one plate on `station.slot1`. The run is a round trip and puts
    the plate back, so repeated runs need no intervention -- but a run that failed part way
    may have left it elsewhere, and putting it back is the operator's job
    (`curl -X POST http://localhost:8001/reseed`).

Exit code 0 means every check passed; anything else is a failure, so this is usable as a
check rather than a demo.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import yaml
from ofplang.run import front_door_check
from ofplang.run.runner import load_document, serialize_document

from labcode.dialect import validate_dialect
from labcode.runner import run_labcode

HERE = Path(__file__).parent
WORKFLOW = HERE / "sila2_seal.workflow.yaml"
ENVIRONMENT = HERE / "sila2_seal.env.yaml"
BOUNDARY = HERE / "sila2_seal.boundary.yaml"

# One real second per environment tick. The environment's durations are written in the same
# units as the reference lab's realistic timing profile, so at 1.0 the two agree and the run
# neither over- nor under-runs badly. Against the lab's default profile (which waits for
# nothing) every op finishes early instead, which is equally fine.
SECONDS_PER_TICK = 1.0


class CheckFailed(Exception):
    """A check on the run's outcome did not hold. Raised rather than asserted so the message
    reaches the operator even when Python runs with assertions disabled."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def validate_front_door(workflow: Path, environment: Path) -> None:
    """Run the same two checks `lc run` does, in the same order, before executing anything.

    `run_labcode` runs trusting -- it assumes the documents already passed the front door -- so
    without this a malformed document would surface as a confusing mid-run failure instead of a
    validation error naming what is wrong. First the portable workflow
    (`ofplang.run.front_door_check`), then labcode's own `x-labcode` extension."""
    front_door = front_door_check(str(workflow), validate=True)
    if not front_door.ok:
        for diagnostic in front_door.diagnostics:
            print(f"  workflow error {diagnostic.code}: {diagnostic.message}", file=sys.stderr)
        if front_door.unsupported is not None:
            print(f"  workflow unsupported: {front_door.unsupported}", file=sys.stderr)
    require(front_door.ok, "the workflow failed the portable ofplang front door")

    dialect = validate_dialect(load_document(str(workflow)), load_document(str(environment)))
    for warning in dialect.warnings:
        print(f"  dialect warning: {warning}")
    for error in dialect.errors:
        print(f"  dialect error: {error}", file=sys.stderr)
    require(
        dialect.ok,
        f"the environment failed labcode's dialect validation ({len(dialect.errors)} error(s))",
    )


def check_outcome(status: dict, result_boundary: dict, observation: list[dict]) -> None:
    """Assert on what a real client can see: the schedule, the produced boundary, the views."""
    # 1. Every activity finished. An activity carries `status`, and an op whose SiLA2 command
    #    failed does not reach `completed` -- so this is what catches a command the instrument
    #    rejected, including a transport that named a location the lab does not have.
    activities = status.get("activities") or []
    require(bool(activities), "the status document contains no activities")
    incomplete = [
        (
            activity.get("kind"),
            activity.get("process") or activity.get("to_spot"),
            activity.get("status"),
        )
        for activity in activities
        if activity.get("status") != "completed"
    ]
    require(not incomplete, f"activities did not complete: {incomplete}")
    # The round trip is two moves and one seal; fewer would mean the plate never travelled.
    kinds = [activity.get("kind") for activity in activities]
    require(kinds.count("transport") == 2, f"expected two transports, got {kinds}")
    require(kinds.count("processing") == 1, f"expected one processing activity, got {kinds}")

    # 2. The measurement came back, and it is a real reading rather than a placeholder: the
    #    instrument reports how many cycles it has performed, and this run performed one.
    outputs = (result_boundary.get("boundary") or {}).get("outputs") or {}
    cycles = (outputs.get("cycle_count") or {}).get("view")
    require(isinstance(cycles, int), f"cycle_count is not an integer reading: {cycles!r}")
    require(cycles >= 1, f"cycle_count should have counted at least this run's cycle: {cycles}")

    # 3. The plate came back to the spot it started from, and it is the same plate. `_id` is
    #    labcode's implicit Object identity, so an id mismatch would mean something recreated
    #    the plate rather than carrying it -- the failure a round trip is able to detect.
    returned_plate = outputs.get("plate") or {}
    require(
        returned_plate.get("spot") == "station.slot1",
        f"the plate did not come back to station.slot1: {returned_plate.get('spot')!r}",
    )
    returned_id = (returned_plate.get("view") or {}).get("_id")
    require(bool(returned_id), f"the returned plate carries no _id: {returned_plate!r}")

    # 4. The seal handled that same plate. The observation is one document per activity, so the
    #    seal is found by its process name rather than by an id.
    seals = [entry for entry in observation if entry.get("process") == "seal"]
    require(len(seals) == 1, f"expected one seal in the observation, found {len(seals)}")
    # Each port is recorded as `{view: ...}`, and only an Object's view is a mapping carrying
    # `_id` -- `cycle_count`'s view is the number itself.
    seen = {
        port["view"]["_id"]
        for section in ("inputs", "outputs")
        for port in ((seals[0].get(section) or {}).values())
        if isinstance(port, dict) and isinstance(port.get("view"), dict) and port["view"].get("_id")
    }
    require(
        seen == {returned_id},
        f"the seal saw plate ids {sorted(seen)}, but {returned_id} came back",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seconds-per-tick",
        type=float,
        default=SECONDS_PER_TICK,
        help=f"real seconds per environment tick (default {SECONDS_PER_TICK:g})",
    )
    parser.add_argument(
        "--artifacts",
        metavar="DIR",
        help="write the status, observation and result boundary here"
        " (default: a temporary directory)",
    )
    arguments = parser.parse_args(argv)

    # Artifacts go to a temporary directory by default and are reported only when something
    # went wrong: their contents depend on real wall-clock timing, so they are evidence for a
    # failure rather than something worth keeping.
    with tempfile.TemporaryDirectory(prefix="sila2_seal-") as temporary:
        artifacts = Path(arguments.artifacts) if arguments.artifacts else Path(temporary)
        artifacts.mkdir(parents=True, exist_ok=True)
        observation_path = artifacts / "observation.yaml"

        print(f"validating {ENVIRONMENT.name} at the front door")
        try:
            validate_front_door(WORKFLOW, ENVIRONMENT)
        except CheckFailed as error:
            print(f"run_sila2_seal: {error}", file=sys.stderr)
            return 1

        print(f"running {WORKFLOW.name} at {arguments.seconds_per_tick:g}s per tick")
        boundary = load_document(str(BOUNDARY))
        try:
            result = run_labcode(
                str(WORKFLOW),
                str(ENVIRONMENT),
                boundary,
                # Mirror `lc run`'s cadence: a running-task margin of at least the poll
                # interval, so an op that overruns its estimate does not get a successor
                # dispatched onto a still-busy device.
                poll_interval=1,
                running_task_margin=1,
                random_seed=0,
                seconds_per_tick=arguments.seconds_per_tick,
                observation_out=str(observation_path),
            )
        except Exception as error:  # noqa: BLE001 - any failure is this script's failure
            print(
                f"run_sila2_seal: the run itself failed: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1

        status = result.status
        (artifacts / "status.yaml").write_text(serialize_document(status), encoding="utf-8")
        (artifacts / "boundary.yaml").write_text(
            yaml.safe_dump(result.result_boundary, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        # A stream, not a single document: a header followed by one document per activity.
        observation: list[dict] = []
        if observation_path.is_file():
            observation = [
                document
                for document in yaml.safe_load_all(observation_path.read_text(encoding="utf-8"))
                if isinstance(document, dict) and document.get("kind")
            ]

        print(f"makespan        : {status.get('now')}")
        print(f"result boundary : {result.result_boundary}")

        try:
            check_outcome(status, result.result_boundary, observation)
        except CheckFailed as error:
            print(f"run_sila2_seal: {error}", file=sys.stderr)
            if not arguments.artifacts:
                # The temporary directory is about to disappear, so say what was in it.
                print(
                    "run_sila2_seal: rerun with --artifacts DIR to keep the run's documents",
                    file=sys.stderr,
                )
            return 1

    print("sila2_seal integration check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
