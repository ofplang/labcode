"""Tests for what a run records (the `labcode.backend` / `labcode.runner` wiring).

A recorder is injected rather than installed, so these check the wiring -- which operations
are recorded, what identifies them, what closes them and with what -- in an interpreter with
no recording backend at all. What the OpenTelemetry side then does with it is
`test_otel.py`'s.

The end-to-end tests drive `transport.workflow.yaml`: `source` **creates** a Sample,
a transport moves it, `target` consumes it. That covers the three cases that differ -- an
operation whose Object only exists once it finishes, one that carries an Object it was given,
and a transport -- in one run.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from ofplang.run.simulator import SimulatorError

from labcode.backend import LabcodeBackend, make_transport_resolver
from labcode.idgen import RealUuid4Generator, SeededUuid4Generator
from labcode.record import (
    ATTR_MODE,
    ATTR_NODE,
    ATTR_OBJECT_ID,
    ATTR_PROCESS,
    ATTR_SPOT_FROM,
    ATTR_SPOT_TO,
    ATTR_TICK_END,
    ATTR_TICK_START,
    ATTR_TRANSPORTER,
    RUN_STOPPED,
    SPAN_TRANSPORT,
)

FIX = Path(__file__).parent / "fixtures"
TWF = str(FIX / "transport.workflow.yaml")
TENV = str(FIX / "transport.env.yaml")

TRANSPORT_ENV = {
    "time": {"unit": "second"},
    "devices": [{"id": "s0", "spots": ["core"]}, {"id": "s1", "spots": ["core"]}],
    "transporters": [{"id": "t"}],
    "transports": [
        {
            "transporter": "t", "from": "s0.core", "to": "s1.core", "duration": 1,
            "x-labcode": {"script": {"language": "python", "code": "MOVE"}},
        }
    ],
    "processes": {},
    "objective": {"kind": "makespan"},
}


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class NeverFinishes:
    """A child handle that never exits, so an operation stays running."""

    returncode = None
    stderr = None
    stdin = None

    def poll(self):
        return None

    def terminate(self):
        pass


class FakeRecorder:
    """Watches what a run records."""

    def __init__(self, trace_id: str = "0123456789abcdef0123456789abcdef") -> None:
        self.trace_id = trace_id
        self.events: list[tuple] = []
        self.names: dict[str, str] = {}
        self.started: dict[str, dict] = {}
        self.finished: dict[str, tuple] = {}
        self.active: list[str] = []
        self.child_envs = 0

    def run_started(self, *, mission_id=None):
        self.events.append(("run_started", mission_id))
        return self.trace_id

    def run_finished(self, *, error_type=None, message=None):
        self.events.append(("run_finished", error_type))

    def op_started(self, uuid, name, attributes):
        self.events.append(("op_started", name))
        self.names[uuid] = name
        self.started[uuid] = dict(attributes)

    @contextlib.contextmanager
    def op_active(self, uuid):
        self.active.append(uuid)
        try:
            yield
        finally:
            self.active.pop()

    def op_finished(self, uuid, *, error_type=None, message=None, attributes=None):
        self.events.append(("op_finished", self.names.get(uuid), error_type))
        self.finished[uuid] = (error_type, message, dict(attributes or {}))

    def child_env(self):
        self.child_envs += 1
        return {"TRACEPARENT": f"00-{self.trace_id}-abcdefabcdefabcd-01"}

    def shutdown(self):
        self.events.append(("shutdown",))

    # -- reading the record back ---------------------------------------------------

    def record(self, name: str) -> tuple[dict, str | None, str | None]:
        """``(attributes, error type, message)`` of the one operation recorded as `name`.

        Attributes are as the record ends up: what completion added replaces what dispatch
        knew, which is how an Object minted at completion appears."""
        keys = [key for key, recorded in self.names.items() if recorded == name]
        assert len(keys) == 1, f"expected exactly one {name!r}, got {keys}"
        error_type, message, final = self.finished[keys[0]]
        return {**self.started[keys[0]], **final}, error_type, message

    def is_open(self, name: str) -> bool:
        keys = [key for key, recorded in self.names.items() if recorded == name]
        return bool(keys) and keys[0] not in self.finished


def _run(environment: str = TENV, **kwargs):
    """Drive the transport fixture to completion with a watching recorder."""
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    recorder = FakeRecorder()
    clock = FakeClock()
    runner = LabcodeRunner(
        TWF, environment, seconds_per_tick=0.001, monotonic=clock.monotonic,
        sleep=clock.sleep, random_seed=0, running_task_margin=1, recorder=recorder, **kwargs,
    )
    try:
        runner.run()
    finally:
        runner.sim.close()
    return runner, recorder


# -- the run and its operations ---------------------------------------------------------


def test_a_run_records_every_operation_it_dispatched():
    runner, recorder = _run(mission_id="M-2026-001")

    assert not runner.failed
    assert recorder.events[0] == ("run_started", "M-2026-001")
    assert ("run_finished", None) in recorder.events
    assert recorder.events[-1] == ("shutdown",)
    assert sorted(recorder.names.values()) == ["process source", "process target", "transport"]
    assert not recorder.active, "every dispatch window was left"


def test_an_operation_points_back_at_the_workflow_and_at_the_plan():
    _runner, recorder = _run()

    attributes, error_type, _message = recorder.record("process source")
    assert error_type is None
    assert attributes[ATTR_PROCESS] == "source"
    assert ATTR_MODE in attributes
    assert attributes[ATTR_NODE].endswith("SampleSource")
    # The plan's interval, recorded beside the real timestamps the record keeps itself.
    assert attributes[ATTR_TICK_END] > attributes[ATTR_TICK_START]


def test_a_transport_records_the_route_it_took():
    _runner, recorder = _run()

    attributes, error_type, _message = recorder.record(SPAN_TRANSPORT)
    assert error_type is None
    assert attributes[ATTR_SPOT_FROM] == "station_0.core"
    assert attributes[ATTR_SPOT_TO] == "station_1.core"
    assert attributes[ATTR_TRANSPORTER] == "transport"
    assert ATTR_NODE not in attributes, "a transport comes from an arc, not a node"


def test_one_object_is_followed_through_the_run():
    """The Sample `source` creates has no identity until it finishes -- and it is the same
    Sample the transport carried and `target` consumed."""
    _runner, recorder = _run()

    created, _error, _message = recorder.record("process source")
    moved, _error, _message = recorder.record(SPAN_TRANSPORT)
    consumed, _error, _message = recorder.record("process target")

    identities = created[ATTR_OBJECT_ID]
    assert isinstance(identities, list) and len(identities) == 1
    assert moved[ATTR_OBJECT_ID] == identities
    assert consumed[ATTR_OBJECT_ID] == identities


def test_a_failed_operation_records_the_reason_the_backend_gave(tmp_path):
    environment = tmp_path / "env.yaml"
    environment.write_text(
        Path(TENV).read_text(encoding="utf-8").replace(
            "moved = (view, from_spot, to_spot, transporter)",
            "raise RuntimeError('gripper stuck')",
        ),
        encoding="utf-8",
    )

    runner, recorder = _run(str(environment))

    assert runner.failed and runner.failure is not None
    _attributes, error_type, message = recorder.record(SPAN_TRANSPORT)
    assert error_type == "script_error"
    assert "gripper stuck" in (message or "")
    assert ("run_finished", runner.failure.kind) in recorder.events


def test_the_trace_id_is_offered_as_the_run_starts():
    seen: list[str] = []
    runner, recorder = _run(on_trace_id=seen.append)

    assert seen == [recorder.trace_id]
    assert runner.trace_id == recorder.trace_id


# -- Object identities and recording are independent knobs ------------------------------


def test_recording_a_run_mints_real_object_identities():
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    def runner(**kwargs):
        return LabcodeRunner(TWF, TENV, recorder=FakeRecorder(), random_seed=0, **kwargs)

    assert isinstance(runner().id_generator, SeededUuid4Generator)
    assert isinstance(runner(trace=True).id_generator, RealUuid4Generator)
    # Reproducible *and* recorded: asked for explicitly, it wins.
    fixed = runner(trace=True, id_generator=SeededUuid4Generator())
    assert isinstance(fixed.id_generator, SeededUuid4Generator)


# -- what a child process is launched with ----------------------------------------------


def _backend(recorder: FakeRecorder) -> LabcodeBackend:
    return LabcodeBackend(
        TRANSPORT_ENV,
        resolver=lambda *args: None,
        transport_resolver=make_transport_resolver(TRANSPORT_ENV),
        recorder=recorder,
        seconds_per_tick=0.001,
    )


def test_a_child_is_launched_with_what_ties_it_to_its_operation(monkeypatch):
    import labcode.backend as backend_module

    captured: dict = {}

    def spawn(job, env=None):
        captured["job"], captured["env"] = job, env
        return NeverFinishes()

    monkeypatch.setattr(backend_module, "_labcode_child_spawn", spawn)
    recorder = FakeRecorder()
    backend = _backend(recorder)
    try:
        backend.place("s0.core")
        backend.dispatch_transport("t", "s0.core", "s1.core")

        assert captured["job"]["code"] == "MOVE"
        assert captured["env"] == {"TRACEPARENT": f"00-{recorder.trace_id}-abcdefabcdefabcd-01"}
        assert recorder.child_envs == 1
        assert recorder.is_open(SPAN_TRANSPORT), "the move is still running"
    finally:
        backend.close()


def test_operations_still_running_when_the_backend_closes_say_so(monkeypatch):
    import labcode.backend as backend_module

    monkeypatch.setattr(
        backend_module, "_labcode_child_spawn", lambda job, env=None: NeverFinishes()
    )
    recorder = FakeRecorder()
    backend = _backend(recorder)
    backend.place("s0.core")
    backend.dispatch_transport("t", "s0.core", "s1.core")

    backend.close()

    _attributes, error_type, _message = recorder.record(SPAN_TRANSPORT)
    assert error_type == RUN_STOPPED


def test_a_dispatch_the_oracle_refuses_is_recorded_as_refused(monkeypatch):
    import labcode.backend as backend_module

    monkeypatch.setattr(
        backend_module, "_labcode_child_spawn", lambda job, env=None: NeverFinishes()
    )
    recorder = FakeRecorder()
    backend = _backend(recorder)
    try:
        # Nothing is on the source spot, so there is nothing to move.
        with pytest.raises(SimulatorError):
            backend.dispatch_transport("t", "s0.core", "s1.core")

        _attributes, error_type, message = recorder.record(SPAN_TRANSPORT)
        assert error_type is not None, "a refused dispatch must not look like it is still running"
        assert message
    finally:
        backend.close()
