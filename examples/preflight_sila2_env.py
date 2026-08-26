"""Check a labcode environment against the machines it names -- **without commanding any of them**.

This is what to run before the first run on a bench. It answers the questions that otherwise
get answered by a plate in the wrong place:

  1. is every machine the environment declares reachable, and does labcode's own client build
     succeed against it (which is where TLS, a wrong port and a half-open server show up)?
  2. does each machine serve the Features its scripts name? A script that reaches for
     `sila2_client.PlateLocController` on a server that does not implement it fails at the
     moment of use -- mid-run, with a plate somewhere.
  3. are the station names in the transport scripts names the transporter actually knows?
     `CarriageService.StationNames` lists them, and a name that is not in that list fails with
     `InvalidStation`.

WHAT IT DOES NOT DO: by default it issues **no SiLA2 command**. Everything in the default run
is a read -- building a client (which fetches Feature definitions), the `SiLAService` properties
every SiLA2 server serves, and the transporter's non-observable `StationNames` property. Nothing
moves, nothing opens, nothing starts. That is the whole point: it is the check you are allowed
to run on a bench you have not been given permission to drive yet.

`--arm-state` adds one more question -- *is the arm in a state where a transfer is possible?* --
and it is opt-in because answering it means **calling commands**. They are queries: `GetMode`
returns the operating mode, `IsAtBasePose` / `IsAtRetractPose` answer where the robot is, and
none of them actuates anything. But a query that is formally a Command is still a Command, so it
is asked for explicitly rather than folded into the default. It is worth asking right before a
run, because a controller that was restarted may not be at a known pose, and `Transfer` refuses
one that is not (`RobotNotAtKnownPose`).

WHAT IT CANNOT CHECK is the one thing hardware will not catch either: whether a station name
the machine *does* know means the place the workflow means. `Base4` that is the sealer here and
the cycler there does not fail -- it moves a plate somewhere else. Confirm the mapping against
the bench.

Usage:

    python examples/preflight_sila2_env.py --env examples/sila2_plate_cycle_no_atc.remote.env.yaml

Exit code 0 means every machine answered, served what its scripts name, and knew every station
name they use. Anything else is a finding, printed with the machine it belongs to.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from labcode.sila2 import connect

#: `sila2_client.SomeFeature` -- the machine the mode or route is *for* (the mode's device, or
#: the route's transporter).
OWN_CLIENT_FEATURE = re.compile(r"\bsila2_client\.([A-Za-z_][A-Za-z0-9_]*)")
#: `sila2_clients["device_id"].SomeFeature` -- a route's endpoint device (`endpoints: true`).
ENDPOINT_CLIENT_FEATURE = re.compile(
    r"""\bsila2_clients\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]\.([A-Za-z_][A-Za-z0-9_]*)"""
)
#: The station names a transport script hands the transporter.
STATION_ARGUMENT = re.compile(r"""\b(?:Source|Destination)Station\s*=\s*["']([^"']+)["']""")

#: Every SiLA2 server serves this, and it is not something a script names.
ALWAYS_SERVED = "SiLAService"
#: The Feature whose `StationNames` property lists what the transporter will accept, and whose
#: `CarriagePosition` says where the carriage is now.
CARRIAGE_FEATURE = "CarriageService"
#: `--arm-state` only: (Feature, member, kind). "command" members are unobservable Commands that
#: report state and actuate nothing; "property" members are plain reads. Nothing here moves the
#: machine, which is why it is askable at all -- but the Commands are why it is opt-in.
STATE_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("ConnectionService", "GetMode", "command"),
    ("RobotPoseService", "IsAtBasePose", "command"),
    ("RobotPoseService", "IsAtRetractPose", "command"),
    (CARRIAGE_FEATURE, "CarriagePosition", "property"),
)


class Machine:
    """One declared connection, and what the environment's scripts expect of it."""

    def __init__(self, identifier: str, role: str, connection: dict) -> None:
        self.id = identifier
        self.role = role  # "device" or "transporter", for the report only
        # A malformed connection is the front door's to reject, so these are taken as they
        # come and stringified for the report; what they mean to a client is settled by
        # `connect` failing.
        self.host = str(connection.get("host"))
        self.port = str(connection.get("port"))
        self.insecure = bool(connection.get("insecure", False))
        self.features: set[str] = set()
        self.stations: set[str] = set()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


def collect_machines(environment: dict) -> dict[str, Machine]:
    """Every device and transporter that declares an `x-labcode.connection`.

    A device without one is a shelf rather than an instrument (the station slot in these
    examples), so it is not a machine to check."""
    machines: dict[str, Machine] = {}
    for role, key in (("device", "devices"), ("transporter", "transporters")):
        for entry in environment.get(key) or []:
            if not isinstance(entry, dict):
                continue
            connection = ((entry.get("x-labcode") or {}).get("connection")) or {}
            if connection:
                machines[entry["id"]] = Machine(entry["id"], role, connection)
    return machines


def script_of(holder: dict) -> str:
    """The `x-labcode.script.code` of a mode or a route, or the empty string."""
    script = (holder.get("x-labcode") or {}).get("script") or {}
    code = script.get("code")
    return code if isinstance(code, str) else ""


def collect_expectations(environment: dict, machines: dict[str, Machine]) -> list[str]:
    """Fill in each machine's expected Features and station names, reading the scripts.

    Returns the problems found in the *environment itself* -- a script naming a client for a
    device that declares no connection, say -- which are worth reporting before anything is
    contacted, since no amount of hardware will fix them."""
    problems: list[str] = []

    def note_own(code: str, owner: str | None) -> None:
        features = set(OWN_CLIENT_FEATURE.findall(code))
        if not features:
            return
        if owner is None or owner not in machines:
            problems.append(
                f"a script uses `sila2_client` but its machine {owner!r} declares no connection"
            )
            return
        machines[owner].features |= features

    def note_endpoints(code: str) -> None:
        for device, feature in ENDPOINT_CLIENT_FEATURE.findall(code):
            if device not in machines:
                problems.append(
                    f"a script reaches for `sila2_clients[{device!r}]`, which declares no"
                    " connection"
                )
                continue
            machines[device].features.add(feature)

    for name, process in (environment.get("processes") or {}).items():
        for index, mode in enumerate(process.get("modes") or []):
            code = script_of(mode)
            if not code:
                continue
            devices = mode.get("devices") or []
            if len(devices) == 1:
                note_own(code, devices[0])
            elif OWN_CLIENT_FEATURE.search(code):
                # With more than one device the flavor's `sila2_client` is not this simple, and
                # guessing which machine a Feature belongs to would be worse than saying so.
                problems.append(
                    f"processes.{name}.modes[{index}] has {len(devices)} devices, so this check"
                    " cannot tell which one its `sila2_client` Features belong to"
                )
            note_endpoints(code)

    for route in environment.get("transports") or []:
        code = script_of(route)
        if not code:
            continue
        note_own(code, route.get("transporter"))
        note_endpoints(code)
        stations = set(STATION_ARGUMENT.findall(code))
        if stations:
            transporter = route.get("transporter")
            if transporter in machines:
                machines[transporter].stations |= stations

    return problems


def served_features(client: Any) -> set[str]:
    """The Feature identifiers a server implements, by their short name.

    `ImplementedFeatures` is a property of the mandatory `SiLAService` Feature, so reading it
    commands nothing. It returns fully qualified identifiers
    (`org.silastandard/core/SiLAService/v1`), of which the workflow-facing name is the third
    segment -- that is what a script writes."""
    value = client.SiLAService.ImplementedFeatures.get()
    identifiers = value if isinstance(value, (list, tuple)) else [value]
    names = set()
    for identifier in identifiers:
        parts = str(identifier).split("/")
        names.add(parts[-2] if len(parts) >= 2 else str(identifier))
    return names


def describe(client: Any) -> str:
    """A line of server identity, all of it `SiLAService` property reads."""
    service = client.SiLAService
    try:
        return (
            f"{service.ServerName.get()} / {service.ServerType.get()}"
            f" v{service.ServerVersion.get()} ({service.ServerUUID.get()})"
        )
    except Exception as error:  # noqa: BLE001 - identity is a nicety, not the check
        return f"(server identity unreadable: {type(error).__name__}: {error})"


def check_machine(machine: Machine, *, read_state: bool = False) -> list[str]:
    """Connect to one machine, read what it serves, and report what does not line up."""
    findings: list[str] = []
    print(f"\n{machine.id} ({machine.role}) at {machine.address}")

    if not machine.insecure:
        # labcode refuses TLS at the front door, so this environment would not run at all.
        findings.append(f"{machine.id}: connection.insecure is not true, which labcode refuses")
        print("  skipped: TLS is not supported")
        return findings

    try:
        client = connect(machine.host, int(machine.port), insecure=True)
    except Exception as error:  # noqa: BLE001 - any failure to connect is a finding
        findings.append(f"{machine.id}: cannot build a client: {type(error).__name__}: {error}")
        print(f"  UNREACHABLE: {type(error).__name__}: {error}")
        return findings

    print(f"  connected: {describe(client)}")

    try:
        available = served_features(client)
    except Exception as error:  # noqa: BLE001
        findings.append(
            f"{machine.id}: cannot read ImplementedFeatures: {type(error).__name__}: {error}"
        )
        print(f"  ImplementedFeatures unreadable: {type(error).__name__}: {error}")
        return findings

    print(f"  serves ({len(available)}): {', '.join(sorted(available))}")

    wanted = machine.features - {ALWAYS_SERVED}
    missing = sorted(wanted - available)
    if missing:
        findings.append(f"{machine.id}: scripts name Features it does not serve: {missing}")
        print(f"  MISSING for the scripts: {missing}")
    elif wanted:
        print(f"  the scripts' Features are served: {', '.join(sorted(wanted))}")

    if machine.stations:
        findings += check_stations(machine, client, available)

    if read_state and machine.role == "transporter":
        report_state(client, available)

    return findings


def report_state(client: Any, available: set[str]) -> None:
    """Print what the transporter says about its own state. Reports, never fails.

    Nothing here is a finding: what a good state looks like is the machine's business and the
    operator's, and a workflow that cannot run because the robot is not homed says so at the
    first `Transfer` with a named error. This is here so that answer is available *before* the
    plate is in the air."""
    print("  state (queries only -- nothing here actuates):")
    for feature, member, kind in STATE_QUERIES:
        if feature not in available:
            print(f"    {feature}.{member}: not served")
            continue
        try:
            handle = getattr(getattr(client, feature), member)
            answer = handle.get() if kind == "property" else handle()
        except Exception as error:  # noqa: BLE001 - a state read is a nicety, not the check
            print(f"    {feature}.{member}: unreadable ({type(error).__name__}: {error})")
            continue
        print(f"    {feature}.{member}: {answer}")


def check_stations(machine: Machine, client: Any, available: set[str]) -> list[str]:
    """Compare the station names the scripts use with the ones the machine lists.

    `CarriageService.StationNames` is a non-observable property: reading it moves nothing."""
    used = sorted(machine.stations)
    if CARRIAGE_FEATURE not in available:
        print(
            f"  station names used by the scripts: {used}"
            f" -- not checkable, {CARRIAGE_FEATURE} is not served"
        )
        return [
            f"{machine.id}: the scripts name stations but the machine does not serve"
            f" {CARRIAGE_FEATURE}, so they cannot be checked"
        ]

    try:
        value = getattr(client, CARRIAGE_FEATURE).StationNames.get()
    except Exception as error:  # noqa: BLE001
        print(f"  StationNames unreadable: {type(error).__name__}: {error}")
        return [f"{machine.id}: cannot read StationNames: {type(error).__name__}: {error}"]

    known = [str(name) for name in (value if isinstance(value, (list, tuple)) else [value])]
    print(f"  knows {len(known)} station(s): {', '.join(known)}")
    print(f"  the scripts use: {', '.join(used)}")
    unknown = sorted(set(used) - set(known))
    if unknown:
        print(f"  UNKNOWN to the machine: {unknown}")
        return [f"{machine.id}: the scripts use station names it does not know: {unknown}"]
    print("  every station name the scripts use is one the machine knows")
    print(
        "  NOTE: that they are known does not mean each one is the place the workflow means."
        " Confirm the mapping against the bench."
    )
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--env", metavar="ENV", required=True, help="the labcode environment to check"
    )
    parser.add_argument(
        "--arm-state",
        action="store_true",
        help="also ask each transporter for its operating mode and whether it is at a known"
        " pose. These are SiLA2 Commands -- queries that actuate nothing, but Commands -- so"
        " they are opt-in; without this flag no command is issued at all",
    )
    arguments = parser.parse_args(argv)

    path = Path(arguments.env)
    if not path.is_file():
        print(f"preflight_sila2_env: environment not found: {path}", file=sys.stderr)
        return 1
    environment = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(environment, dict):
        print(f"preflight_sila2_env: {path} is not a mapping", file=sys.stderr)
        return 1

    machines = collect_machines(environment)
    if not machines:
        print(f"preflight_sila2_env: {path.name} declares no connections to check")
        return 1

    issued = (
        "state queries will be issued to each transporter"
        if arguments.arm_state
        else "no command will be issued"
    )
    print(f"{path.name}: {len(machines)} machine(s) declared -- {issued}")
    findings = collect_expectations(environment, machines)
    for finding in findings:
        print(f"  environment: {finding}")

    for machine in machines.values():
        findings += check_machine(machine, read_state=arguments.arm_state)

    print("\n=== summary ===")
    if findings:
        for finding in findings:
            print(f"FAIL  {finding}")
        print(f"\n{len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"PASS  every machine in {path.name} answered and serves what its scripts name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
