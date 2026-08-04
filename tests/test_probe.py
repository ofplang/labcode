"""Tests for availability probing (`labcode.probe`).

Three layers, tested apart: the **policy** (what an environment's `x-labcode.probe` blocks
add up to), the **cache** (how often a machine is actually probed, and what is reported),
and the **end to end** effect -- an unreachable machine is taken out of the plan and the
work goes another way. None of it touches a network: the prober is injected, and the one
test of the real prober uses a socket it opened itself.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from ofplang.run.runner import RunnerError

from labcode.extension import Connection, Probe, merge_probe, parse_probe
from labcode.probe import Availability, Target, build_availability, probe_targets, tcp_reachable
from labcode.runner import LabcodeRunner

CONNECTION = {"kind": "sila2", "host": "127.0.0.1", "port": 50053, "insecure": True}
ARM_CONNECTION = {**CONNECTION, "port": 50057}


def _env(*devices: dict, root: dict | None = None, transporters: list | None = None) -> dict:
    env: dict = {"processes": {}, "devices": list(devices)}
    if transporters is not None:
        env["transporters"] = transporters
    if root is not None:
        env["x-labcode"] = {"probe": root}
    return env


def _device(identifier: str, *, connection: dict | None = CONNECTION, probe: dict | None = None):
    extension: dict = {}
    if connection is not None:
        extension["connection"] = connection
    if probe is not None:
        extension["probe"] = probe
    entry: dict = {"id": identifier, "spots": ["stage"]}
    if extension:
        entry["x-labcode"] = extension
    return entry


# -- the policy ----------------------------------------------------------------


def test_a_policy_declares_only_what_it_says():
    declared, errors = parse_probe({"interval": 60})
    assert declared == {"interval": 60.0}
    assert not errors


def test_once_is_carried_as_no_repeat():
    declared, errors = parse_probe({"interval": "once"})
    assert declared == {"interval": None}
    assert not errors


def test_layers_merge_field_by_field():
    root = {"enabled": True, "timeout": 2.0, "interval": 60.0}
    assert merge_probe(root, {"interval": None}) == Probe(enabled=True, timeout=2.0, interval=None)


def test_the_default_policy_is_off():
    assert merge_probe({}) == Probe(enabled=False, timeout=5.0, interval=None)


def test_only_enabled_machines_with_an_address_are_probed():
    env = _env(
        _device("reader", probe={"enabled": True}),
        _device("sealer"),  # a connection, but no policy enables it
        _device("rack", connection=None, probe={"enabled": True}),  # nothing to probe
        root=None,
    )
    assert [target.identifier for target in probe_targets(env)] == ["reader"]


def test_a_root_policy_reaches_every_machine_that_has_an_address():
    env = _env(
        _device("reader"),
        _device("sealer"),
        _device("rack", connection=None),
        root={"enabled": True},
        transporters=[{"id": "arm", "x-labcode": {"connection": ARM_CONNECTION}}],
    )
    # Devices in document order, then transporters.
    assert [target.identifier for target in probe_targets(env)] == ["reader", "sealer", "arm"]


def test_a_machine_can_opt_out_of_a_root_policy():
    env = _env(
        _device("reader"),
        _device("sealer", probe={"enabled": False}),
        root={"enabled": True},
    )
    assert [target.identifier for target in probe_targets(env)] == ["reader"]


def test_a_machine_overrides_one_field_of_the_root_policy():
    env = _env(_device("reader", probe={"interval": 30}), root={"enabled": True, "timeout": 2})
    (target,) = probe_targets(env)
    assert target.policy == Probe(enabled=True, timeout=2.0, interval=30.0)


def test_no_policy_at_all_means_no_availability_object():
    # The feature costs a run nothing until it is asked for.
    assert build_availability(_env(_device("reader"))) is None


# -- the cache -----------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _targets(*identifiers: str, interval: float | None = None, timeout: float = 5.0):
    return [
        Target(
            identifier,
            Probe(enabled=True, timeout=timeout, interval=interval),
            Connection(host="127.0.0.1", port=50053 + index, insecure=True),
        )
        for index, identifier in enumerate(identifiers)
    ]


def _recording_prober(reachable):
    """A prober that answers from `reachable` (a dict or a callable) and records its calls."""
    calls: list[tuple[str, int, float]] = []

    def prober(host, port, timeout):
        calls.append((host, port, timeout))
        answer = reachable(port) if callable(reachable) else reachable
        return answer

    return prober, calls


def test_once_probes_a_machine_a_single_time():
    prober, calls = _recording_prober(False)
    clock = _Clock()
    availability = Availability(_targets("reader"), prober=prober, monotonic=clock)

    assert availability.down() == {"reader"}
    clock.now = 10_000.0
    assert availability.down() == {"reader"}  # the cached answer
    assert len(calls) == 1


def test_zero_probes_on_every_round():
    prober, calls = _recording_prober(True)
    availability = Availability(_targets("reader", interval=0), prober=prober, monotonic=_Clock())
    for _ in range(3):
        availability.down()
    assert len(calls) == 3


def test_an_interval_probes_again_only_once_it_is_due():
    prober, calls = _recording_prober(True)
    clock = _Clock()
    availability = Availability(_targets("reader", interval=60), prober=prober, monotonic=clock)

    availability.down()
    clock.now = 59.0
    availability.down()
    assert len(calls) == 1
    clock.now = 60.0
    availability.down()
    assert len(calls) == 2


def test_a_machine_that_comes_back_leaves_the_down_set():
    state = {"up": False}
    prober, _calls = _recording_prober(lambda _port: state["up"])
    availability = Availability(_targets("reader", interval=0), prober=prober, monotonic=_Clock())

    assert availability.down() == {"reader"}
    state["up"] = True
    assert availability.down() == set()


def test_one_probe_per_address_per_round():
    # Two machines on one server: connecting twice would only add delay.
    targets = [
        Target(name, Probe(enabled=True, interval=0), Connection(host="h", port=1, insecure=True))
        for name in ("reader", "sealer")
    ]
    prober, calls = _recording_prober(False)
    availability = Availability(targets, prober=prober, monotonic=_Clock())
    assert availability.down() == {"reader", "sealer"}
    assert len(calls) == 1


def test_the_policy_timeout_is_passed_to_the_prober():
    prober, calls = _recording_prober(True)
    Availability(_targets("reader", timeout=1.5), prober=prober, monotonic=_Clock()).down()
    assert calls[0][2] == 1.5


def test_changes_are_reported_but_an_expected_start_is_not():
    changes: list[tuple[str, bool]] = []
    state = {"up": True}
    prober, _calls = _recording_prober(lambda _port: state["up"])
    availability = Availability(
        _targets("reader", interval=0), prober=prober, monotonic=_Clock(),
        on_change=lambda identifier, reachable: changes.append((identifier, reachable)),
    )

    availability.down()
    assert changes == []  # reachable from the start is what was expected: no news
    state["up"] = False
    availability.down()
    assert changes == [("reader", False)]
    state["up"] = True
    availability.down()
    assert changes == [("reader", False), ("reader", True)]


def test_starting_out_unreachable_is_reported():
    changes: list[tuple[str, bool]] = []
    prober, _calls = _recording_prober(False)
    Availability(
        _targets("reader"), prober=prober, monotonic=_Clock(),
        on_change=lambda i, r: changes.append((i, r)),
    ).down()
    assert changes == [("reader", False)]


# -- the real prober -----------------------------------------------------------


def test_tcp_reachable_answers_for_a_socket_this_test_owns():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert tcp_reachable("127.0.0.1", port, 2.0) is True
    # The listener is closed now, so the same address is no longer reachable.
    assert tcp_reachable("127.0.0.1", port, 2.0) is False


def test_tcp_reachable_is_false_for_an_unresolvable_host():
    assert tcp_reachable("no-such-host.invalid", 50053, 2.0) is False


# -- end to end: an unreachable machine is routed around ------------------------
#
# What the feature is for. The workflow moves one Sample from `source` to `target`; the
# environments offer a second way to get it there, and **where the Sample ends up** says
# which way was taken. Nothing here starts a child process (every process is scriptless) or
# opens a socket (the prober is injected), so these run in milliseconds.
#
# Note what is *not* asserted: the makespan. These runs pace the wall clock at a
# millisecond per tick, and the backend derives the tick it reached from real elapsed
# time -- so the clock tracks how long the solver took, not the durations in the fixture.

FIXTURES = Path(__file__).parent / "fixtures"
WORKFLOW = str(FIXTURES / "transport.workflow.yaml")
DEVICE_ENV = str(FIXTURES / "reroute_device.env.yaml")
TRANSPORTER_ENV = str(FIXTURES / "reroute_transporter.env.yaml")

#: The ports the fixtures declare, so a test can say which machine is unreachable.
STATION_1, STATION_2 = 50101, 50102
ARM_A, ARM_B = 50111, 50112


def _run(environment: str, *, unreachable: set[int] = frozenset(), probe: bool = True) -> dict:
    """Drive a fixture to completion with `unreachable` ports refusing to be probed."""
    runner = LabcodeRunner(
        WORKFLOW,
        environment,
        seconds_per_tick=0.001,
        random_seed=0,
        probe=probe,
        prober=lambda host, port, timeout: port not in unreachable,
    )
    try:
        return runner.run()
    finally:
        runner.sim.close()


def _destination(status: dict) -> str:
    """Where `target` consumed the Sample -- which is the route the run took."""
    assert all(a["status"] == "completed" for a in status["activities"]), status["activities"]
    target = next(a for a in status["activities"] if a.get("process") == "target")
    return target["input_spots"]["target_in"]


def test_nothing_unreachable_uses_the_cheap_route():
    assert _destination(_run(DEVICE_ENV)) == "station_1.core"


def test_an_unreachable_device_is_routed_around():
    assert _destination(_run(DEVICE_ENV, unreachable={STATION_1})) == "station_2.core"


def test_an_unreachable_transporter_is_routed_around():
    # The other kind of machine. Only arm_a reaches station_1, so losing the arm -- not the
    # station -- is what sends the Sample to station_2 (ofplang-run >= 0.1.11).
    assert _destination(_run(TRANSPORTER_ENV)) == "station_1.core"
    assert _destination(_run(TRANSPORTER_ENV, unreachable={ARM_A})) == "station_2.core"


def test_a_run_with_no_way_left_stops():
    # Both destinations unreachable: the replan has no route and the run fails, rather
    # than dispatching onto a machine that cannot answer.
    with pytest.raises(RunnerError):
        _run(DEVICE_ENV, unreachable={STATION_1, STATION_2})


def test_a_machine_that_comes_back_is_used_again():
    # `interval: 0` re-probes on every replan, so recovery is noticed. station_1 is
    # unreachable only for the first probe, which happens before anything is committed.
    seen: list[int] = []

    def prober(host, port, timeout):
        seen.append(port)
        return not (port == STATION_1 and seen.count(STATION_1) == 1)

    runner = LabcodeRunner(
        WORKFLOW, DEVICE_ENV, seconds_per_tick=0.001, random_seed=0, prober=prober
    )
    try:
        status = runner.run()
    finally:
        runner.sim.close()
    # Back on the cheap route, as if nothing had happened: the second probe found it, and
    # the carry could not have been committed before then (it waits for `source`).
    assert _destination(status) == "station_1.core"


def test_no_probe_treats_every_machine_as_reachable():
    # What `lc run --no-probe` does: the policies are ignored, so the prober is never
    # called and the cheap route is planned even though station_1 would refuse.
    calls: list[int] = []

    def prober(host, port, timeout):
        calls.append(port)
        return False

    runner = LabcodeRunner(
        WORKFLOW, DEVICE_ENV, seconds_per_tick=0.001, random_seed=0, probe=False, prober=prober
    )
    try:
        status = runner.run()
    finally:
        runner.sim.close()
    assert _destination(status) == "station_1.core"
    assert calls == []


def test_the_backend_reports_probe_and_injected_faults_together():
    # `down_devices` is the one answer the runner polls, so both sources appear in it.
    runner = LabcodeRunner(
        WORKFLOW,
        DEVICE_ENV,
        seconds_per_tick=0.001,
        random_seed=0,
        prober=lambda host, port, timeout: port != STATION_1,
    )
    try:
        runner.sim.schedule_device_down(0, "station_0")
        runner.sim.advance(0)
        assert runner.sim.down_devices() == ["station_0", "station_1"]
    finally:
        runner.sim.close()
