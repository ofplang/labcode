"""Drive `sila2_plate_cycle_no_atc` against a running lab of real SiLA2 servers, and check it.

This is `run_sila2_plate_cycle.py` with the thermal cycler taken out and the steps in the
order a bench with an *unsealed* plate can run them: one Plate is sealed, spun down and
unsealed again by three different instruments, carried between them by the arm, and returned to
the spot -- and the state -- it started in. It is the variant meant to be taken to a bench
where the automated thermal cycler is not available; the full circuit is still the reference,
and both are checked against the same reference lab.

What it can check is exactly what the full circuit can, minus the cycler's reading: every
activity completed, the two remaining readings came back real, the plate came home to
`station.slot1`, and all three instruments handled the *same* plate (its `_id` survives three
handovers).

The instrument arguments come from the delivery scripts in `ScriptsForIntegrationTest`
(snip_xpeel.py, snip_plateloc.py, snip_microplate_centrifuge.py), so what the instruments are
asked to do is what those ask of the real hardware.

VERIFIED AGAINST: ofplang-sila2-backend branch ardea (commit de3c4fd). That lab is a *reference*,
not a requirement: the environment speaks plain SiLA2 and can be pointed at real instruments
by changing the hosts and ports in it. The version is recorded so a run without hardware has
something known to reproduce against -- it is deliberately NOT asserted on, because checking a
server's name or version would be the one thing that stops this script working against the
instruments it is meant to be portable to.

WHAT IT DOES NOT DO: it never contacts the reference lab's world-state service. That service
stands in for the physical world, which exposes no such interface, so a client that read it
could not be pointed at hardware. Everything asserted below comes from what a real client can
see: labcode's own status and observation documents, the produced boundary, and readings the
instruments themselves report.

Prerequisites:

  * the lab is up (`docker compose up -d` in ofplang-sila2-backend). The thermal cycler may be
    up or down: this environment does not declare it, so nothing here connects to it -- which
    is the point of the variant;
  * `sila2` is installed in *this* interpreter's environment -- labcode runs each script in a
    child process launched with `sys.executable`, so the client library has to be importable
    there (`uv sync --extra sila2`, or `pip install 'labcode[sila2]'`);
  * one plate is on `station.slot1`. The run is a round trip and puts the plate back, so
    repeated runs need no intervention (`curl -X POST http://localhost:8001/reseed` restores
    it otherwise).

    No particular door state is needed. The environment treats an instrument as closed at rest
    and each transport opens what it must (`endpoints: true`, §1.6), so the circuit starts
    equally well from the lab's all-open t=0 and from the state a previous run leaves behind --
    which is what makes a second run evidence that the transports really can open an
    instrument.

    🔴 What a *failed* run can leave behind is the plate inside a closed centrifuge. It is an
    operator's to retrieve, as for any run that stops half way; reseeding is the shortcut.

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
WORKFLOW = HERE / "sila2_plate_cycle_no_atc.workflow.yaml"
BOUNDARY = HERE / "sila2_plate_cycle_no_atc.boundary.yaml"
# Only one environment for this workflow, and it is the `flavor: sila2` one. The `raw`
# alternative is demonstrated by sila2_seal, which exists to be that low-level reference.
ENVIRONMENT = HERE / "sila2_plate_cycle_no_atc.wrapped.env.yaml"

# The three processes the circuit runs, in order. Named here because the checks below assert on
# all of them -- that every one ran, and that they all handled the same Plate.
PROCESSES = ("seal", "rotate", "peel")
# Four moves: in to the sealer, between each pair of instruments, and back to the station.
TRANSPORT_COUNT = 4

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


def report_availability(machine: str, reachable: bool) -> None:
    """Say when a machine's reachability changes, the way ``lc run`` does.

    The bundled environment declares no `x-labcode.probe` policy, so nothing is probed and this
    is never called. It is wired up anyway, because the alternative is silence: point `--env` at
    a copy that enables probing and the run then names the machine it lost, rather than failing
    with a no-route the operator has to account for."""
    state = "reachable again" if reachable else "unreachable (probe)"
    print(f"run_sila2_plate_cycle_no_atc: {machine!r} is {state}", file=sys.stderr)


def report_trace_id(trace_id: str) -> None:
    """Say which trace this run was recorded as, the way ``lc run --trace`` does.

    Printed to stderr so a caller reading this script's stdout is unaffected. With a SigNoz at
    the OTLP endpoint, the run is then at `http://localhost:3301/trace/<id>`."""
    print(
        f"run_sila2_plate_cycle_no_atc: recording this run as trace {trace_id}",
        file=sys.stderr,
    )


def report_cadence_slip(skipped: int, budget: float, spent: float) -> None:
    """Say once that a poll cycle outgrew its poll period (SPECIFICATIONS.md §3.1).

    The run is not wrong when this happens -- it skipped the ticks it could not observe -- but
    the cadence being delivered is not the one asked for, and only the caller can fix that."""
    print(
        f"run_sila2_plate_cycle_no_atc: a poll cycle took {spent:.3g}s but the poll period is "
        f"{budget:.3g}s, so {skipped} tick(s) went unobserved",
        file=sys.stderr,
    )


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


def object_ids(entry: dict) -> set[str]:
    """The `_id`s of every Object this observation entry saw, on either side.

    Each port is recorded as `{view: ...}`, and only an Object's view is a mapping carrying
    `_id` -- a Pure Data port's view is the value itself."""
    return {
        port["view"]["_id"]
        for section in ("inputs", "outputs")
        for port in ((entry.get(section) or {}).values())
        if isinstance(port, dict) and isinstance(port.get("view"), dict) and port["view"].get("_id")
    }


def check_outcome(status: dict, result_boundary: dict, observation: list[dict]) -> None:
    """Assert on what a real client can see: the schedule, the produced boundary, the views."""
    # 1. Every activity finished. An activity carries `status`, and an op whose SiLA2 command
    #    failed does not reach `completed` -- so this is what catches a command an instrument
    #    rejected, including a transport that named a station the lab does not have.
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
    # The circuit is four moves and three instrument steps; fewer would mean it was cut short.
    kinds = [activity.get("kind") for activity in activities]
    require(
        kinds.count("transport") == TRANSPORT_COUNT,
        f"expected {TRANSPORT_COUNT} transports, got {kinds}",
    )
    require(
        kinds.count("processing") == len(PROCESSES),
        f"expected {len(PROCESSES)} processing activities, got {kinds}",
    )
    # Nothing was scheduled onto the machine this variant does without. A device that is not
    # declared cannot appear here, so this is a check on the *environment* having really been
    # stripped -- the one thing that would make a run of it evidence for the wrong bench.
    scheduled_devices = {
        device
        for activity in activities
        for device in (activity.get("devices") or [])
        if isinstance(device, str)
    }
    require(
        "thermal_cycler" not in scheduled_devices,
        f"the thermal cycler was scheduled after all: {sorted(scheduled_devices)}",
    )

    outputs = (result_boundary.get("boundary") or {}).get("outputs") or {}

    # 2. Every reading came back, and each is a real one rather than a placeholder.
    tape_left = (outputs.get("tape_left") or {}).get("view")
    if not isinstance(tape_left, int):
        raise CheckFailed(f"tape_left is not an integer reading: {tape_left!r}")
    require(tape_left >= 0, f"tape_left should be a non-negative spool reading: {tape_left}")

    cycles = (outputs.get("cycle_count") or {}).get("view")
    if not isinstance(cycles, int):
        raise CheckFailed(f"cycle_count is not an integer reading: {cycles!r}")
    require(cycles >= 1, f"cycle_count should have counted at least this run's cycle: {cycles}")

    # `rotate` reports nothing -- the centrifuge exposes no counter -- so there is no third
    # reading to check, and the cycler's `elapsed_time` left with the cycler.
    require(
        "elapsed_time" not in outputs,
        "elapsed_time is in the boundary, so this is the full circuit rather than the variant",
    )

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

    # 4. All three instruments handled that same plate. The observation is one document per
    #    activity, so each step is found by its process name rather than by an id. This is what
    #    a circuit can check that a single-step run cannot: identity survives every handover.
    for process in PROCESSES:
        entries = [entry for entry in observation if entry.get("process") == process]
        require(
            len(entries) == 1,
            f"expected one {process} in the observation, found {len(entries)}",
        )
        seen = object_ids(entries[0])
        require(
            seen == {returned_id},
            f"{process} saw plate ids {sorted(seen)}, but {returned_id} came back",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--env",
        metavar="ENV",
        default=str(ENVIRONMENT),
        help=f"the environment to check (default: {ENVIRONMENT.name})",
    )
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
    parser.add_argument(
        "--trace",
        action="store_true",
        help="record the run (labcode.record) and print the trace id; the exporter is"
        " configured by OpenTelemetry's own environment variables, e.g."
        " OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318",
    )
    parser.add_argument(
        "--mission-id",
        metavar="ID",
        help="group this run with others under one mission id (recorded only with --trace)",
    )
    arguments = parser.parse_args(argv)
    environment = Path(arguments.env)
    if not environment.is_file():
        print(
            f"run_sila2_plate_cycle_no_atc: environment not found: {arguments.env}", file=sys.stderr
        )
        return 1

    # Artifacts go to a temporary directory by default and are reported only when something
    # went wrong: their contents depend on real wall-clock timing, so they are evidence for a
    # failure rather than something worth keeping.
    with tempfile.TemporaryDirectory(prefix="sila2_plate_cycle_no_atc-") as temporary:
        artifacts = Path(arguments.artifacts) if arguments.artifacts else Path(temporary)
        artifacts.mkdir(parents=True, exist_ok=True)
        observation_path = artifacts / "observation.yaml"

        print(f"validating {environment.name} at the front door")
        try:
            validate_front_door(WORKFLOW, environment)
        except CheckFailed as error:
            print(f"run_sila2_plate_cycle_no_atc: {error}", file=sys.stderr)
            return 1

        print(
            f"running {WORKFLOW.name} with {environment.name} "
            f"at {arguments.seconds_per_tick:g}s per tick"
        )
        boundary = load_document(str(BOUNDARY))
        try:
            result = run_labcode(
                str(WORKFLOW),
                str(environment),
                boundary,
                # Mirror `lc run`'s cadence: a running-task margin of at least the poll
                # interval, so an op that overruns its estimate does not get a successor
                # dispatched onto a still-busy device.
                poll_interval=1,
                running_task_margin=1,
                random_seed=0,
                seconds_per_tick=arguments.seconds_per_tick,
                observation_out=str(observation_path),
                # Recording is off unless asked for. A duration is one number per operation;
                # the trace is where the SiLA2 commands, the client builds and the poll waits
                # inside that number are separable, which is what makes it worth looking at
                # when the durations here turn out to be wrong.
                #
                # `trace=True` also switches `_id` minting from the seeded generator to real
                # random uuid4s -- a recorded run is a real one, not a reproducible fixture.
                # The checks below compare ids with each other rather than with fixed values,
                # so none of them depends on which generator ran.
                trace=arguments.trace,
                mission_id=arguments.mission_id,
                on_trace_id=report_trace_id,
                # Availability is reported rather than swallowed: these callbacks are what
                # turns a probing environment's findings into something the operator sees
                # (`run_labcode` says nothing on its own). Harmless here -- the bundled
                # environment declares no probe policy, so nothing is checked.
                on_availability_change=report_availability,
                on_cadence_slip=report_cadence_slip,
            )
        except Exception as error:  # noqa: BLE001 - any failure is this script's failure
            print(
                f"run_sila2_plate_cycle_no_atc: the run itself failed: "
                f"{type(error).__name__}: {error}",
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
            print(f"run_sila2_plate_cycle_no_atc: {error}", file=sys.stderr)
            if not arguments.artifacts:
                # The temporary directory is about to disappear, so say what was in it.
                print(
                    "run_sila2_plate_cycle_no_atc: rerun with --artifacts DIR to keep the run's"
                    " documents",
                    file=sys.stderr,
                )
            return 1

    print(f"sila2_plate_cycle_no_atc integration check passed ({environment.name}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
