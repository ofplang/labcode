"""Tests for the SiLA2 instrumentation (`labcode.sila2_instrument`).

The instrumentation is verified against **stand-ins** for the `sila2` classes and a recording
`Sink`, so what is checked here is the semantics -- which calls become units, what identifies
them, when a unit is closed and with what -- in an interpreter that has neither `sila2` nor a
recording backend installed. That is the same approach `test_sila2.py` takes to the `sila2` script
flavor, and it is what lets these run in CI (which installs only ``.[test]``).

One test does reach for the real `sila2` (skipped when it is absent): the intervention points are
private attributes of that library, so something has to notice when they move.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from labcode.sila2_instrument import (
    ATTR_ADDRESS,
    ATTR_COMMAND,
    ATTR_EXECUTION_UUID,
    ATTR_FEATURE,
    ATTR_FQI,
    ATTR_PORT,
    KIND_CLIENT,
    SPAN_CONNECT,
    UNSETTLED,
    Targets,
    close_open,
    instrument_sila2,
    resolve_targets,
    uninstrument_sila2,
)

SEAL = "sila2 SealerControl.Seal"
SEAL_FQI = "org.silastandard/examples/SealerControl/v1/Command/Seal"


# -- a recording sink -------------------------------------------------------------------


@dataclass
class Handle:
    """One measured unit, as the sink saw it."""

    name: str
    kind: str
    attributes: dict = field(default_factory=dict)
    ends: int = 0
    error_type: str | None = None
    message: str | None = None


class RecordingSink:
    """Records what it was asked to measure. `broken` makes every method raise, to check that a
    failing sink cannot break the calls it is measuring."""

    def __init__(self, *, broken: bool = False) -> None:
        self.log: list[tuple] = []
        self.handles: list[Handle] = []
        self.broken = broken

    def _check(self) -> None:
        if self.broken:
            raise RuntimeError("this sink is broken")

    def start(self, name: str, *, kind: str, attributes: Any) -> Handle:
        self._check()
        handle = Handle(name, kind, dict(attributes))
        self.handles.append(handle)
        self.log.append(("start", name))
        return handle

    def update(self, handle: Handle, attributes: Any) -> None:
        self._check()
        handle.attributes.update(attributes)
        self.log.append(("update", handle.name))

    def end(
        self, handle: Handle, *, error_type: str | None = None, message: str | None = None
    ) -> None:
        self._check()
        handle.ends += 1
        handle.error_type = error_type
        handle.message = message
        self.log.append(("end", handle.name))

    @contextlib.contextmanager
    def active(self, handle: Handle):
        self._check()
        self.log.append(("enter", handle.name))
        try:
            yield
        finally:
            self.log.append(("exit", handle.name))


# -- stand-ins for the sila2 classes ----------------------------------------------------


class NotFinished(Exception):
    """Stands in for `sila2.framework.CommandExecutionNotFinished`."""


class FakeClient:
    """Stands in for `SilaClient`: constructing one *is* the connection."""

    def __init__(self, address, port, *, insecure=False, log=None, fail=None) -> None:
        if log is not None:
            log.append(("connecting", address))
        if fail is not None:
            raise fail
        self.address = address
        self.port = port


class FakeCommandDef:
    def __init__(self, identifier: str, fqi: str | None = None) -> None:
        self._identifier = identifier
        if fqi is not None:
            self.fully_qualified_identifier = fqi


class FakeFeature:
    def __init__(self, identifier: str, client: Any) -> None:
        self._identifier = identifier
        self._parent_client = client


class FakeUnobservable:
    def __init__(self, feature, command_def, *, log, result=None, fail=None) -> None:
        self._parent_feature = feature
        self._wrapped_command = command_def
        self._log = log
        self._result = result
        self._fail = fail

    def __call__(self, *args, **kwargs):
        self._log.append(("call", "unobservable"))
        if self._fail is not None:
            raise self._fail
        return self._result


class FakeInstance:
    """Stands in for `ClientObservableCommandInstance`. `outcomes` is what successive
    `get_responses()` calls do -- a value to return, or an exception to raise."""

    def __init__(self, execution_uuid, outcomes, log) -> None:
        self.execution_uuid = execution_uuid
        self._outcomes = list(outcomes)
        self._log = log

    def get_responses(self, *args, **kwargs):
        self._log.append(("call", "get_responses"))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeObservable:
    def __init__(self, feature, command_def, *, log, instance=None, fail=None) -> None:
        self._parent_feature = feature
        self._wrapped_command = command_def
        self._log = log
        self._instance = instance
        self._fail = fail

    def __call__(self, *args, **kwargs):
        self._log.append(("call", "observable"))
        if self._fail is not None:
            raise self._fail
        return self._instance


def _targets(**overrides: Any) -> Targets:
    fields: dict[str, Any] = {
        "client": FakeClient,
        "unobservable": FakeUnobservable,
        "observable": FakeObservable,
        "instance": FakeInstance,
        "not_finished": NotFinished,
    }
    fields.update(overrides)
    return Targets(**fields)


@pytest.fixture(autouse=True)
def _undo_instrumentation():
    """No test may leave the stand-in classes patched."""
    yield
    uninstrument_sila2()


@pytest.fixture
def sink() -> RecordingSink:
    recording = RecordingSink()
    assert instrument_sila2(recording, targets=_targets())
    return recording


def _feature(sink: RecordingSink, *, fqi: str | None = SEAL_FQI) -> tuple[Any, Any]:
    """A connected client's feature and one command definition, with the connection already
    measured (so a command test's own unit is the last one)."""
    client = FakeClient("127.0.0.1", 50052, insecure=True, log=sink.log)
    return FakeFeature("SealerControl", client), FakeCommandDef("Seal", fqi)


# -- the connection ---------------------------------------------------------------------


def test_connecting_is_measured_with_its_endpoint(sink):
    FakeClient("127.0.0.1", 50052, insecure=True, log=sink.log)

    (handle,) = sink.handles
    assert (handle.name, handle.kind) == (SPAN_CONNECT, KIND_CLIENT)
    assert handle.attributes == {ATTR_ADDRESS: "127.0.0.1", ATTR_PORT: 50052}
    assert (handle.ends, handle.error_type) == (1, None)
    # The connection itself ran inside the window, so a later gRPC instrumentation would land
    # every feature-definition fetch under it.
    assert sink.log == [
        ("start", SPAN_CONNECT),
        ("enter", SPAN_CONNECT),
        ("connecting", "127.0.0.1"),
        ("exit", SPAN_CONNECT),
        ("end", SPAN_CONNECT),
    ]


def test_the_endpoint_is_read_from_named_arguments_too(sink):
    FakeClient(address="incubator-01", port=50053, insecure=True, log=sink.log)

    (handle,) = sink.handles
    assert handle.attributes == {ATTR_ADDRESS: "incubator-01", ATTR_PORT: 50053}


def test_a_refused_connection_is_measured_as_a_failure(sink):
    with pytest.raises(RuntimeError):
        FakeClient("127.0.0.1", 50052, insecure=True, log=sink.log, fail=RuntimeError("refused"))

    (handle,) = sink.handles
    assert (handle.ends, handle.error_type) == (1, "RuntimeError")
    assert "refused" in (handle.message or "")


# -- unobservable commands --------------------------------------------------------------


def test_an_unobservable_command_is_one_unit(sink):
    feature, command_def = _feature(sink)
    command = FakeUnobservable(feature, command_def, log=sink.log, result="ok")

    assert command(37, mode="fast") == "ok"

    handle = sink.handles[-1]
    assert (handle.name, handle.kind) == (SEAL, KIND_CLIENT)
    assert handle.attributes == {
        ATTR_FEATURE: "SealerControl",
        ATTR_COMMAND: "Seal",
        ATTR_FQI: SEAL_FQI,
        ATTR_ADDRESS: "127.0.0.1",
        ATTR_PORT: 50052,
    }
    assert (handle.ends, handle.error_type) == (1, None)
    assert sink.log[-4:] == [
        ("enter", SEAL),
        ("call", "unobservable"),
        ("exit", SEAL),
        ("end", SEAL),
    ]


def test_a_failed_command_is_measured_with_the_exception_name(sink):
    feature, command_def = _feature(sink)
    command = FakeUnobservable(feature, command_def, log=sink.log, fail=ValueError("bad parameter"))

    with pytest.raises(ValueError):
        command()

    handle = sink.handles[-1]
    assert (handle.ends, handle.error_type) == (1, "ValueError")


def test_a_command_definition_without_a_fully_qualified_identifier_is_still_measured(sink):
    feature, command_def = _feature(sink, fqi=None)
    command = FakeUnobservable(feature, command_def, log=sink.log, result="ok")

    assert command() == "ok"

    handle = sink.handles[-1]
    assert ATTR_FQI not in handle.attributes
    assert handle.attributes[ATTR_COMMAND] == "Seal"


# -- observable commands ----------------------------------------------------------------


def _observable(sink, outcomes, *, uuid="3f2b8c1a-5555-4666-8777-888899990000"):
    feature, command_def = _feature(sink)
    instance = FakeInstance(uuid, outcomes, sink.log)
    command = FakeObservable(feature, command_def, log=sink.log, instance=instance)
    return command, instance


def test_an_observable_command_stays_open_until_its_responses_are_collected(sink):
    command, instance = _observable(sink, ["responses"])

    assert command() is instance
    handle = sink.handles[-1]
    assert handle.ends == 0, "the instrument is still working"
    assert handle.attributes[ATTR_EXECUTION_UUID] == "3f2b8c1a-5555-4666-8777-888899990000"
    # The window closed with the initiating call: the script code that follows is not adopted.
    assert sink.log[-3:] == [("call", "observable"), ("exit", SEAL), ("update", SEAL)]

    assert instance.get_responses() == "responses"
    assert (handle.ends, handle.error_type) == (1, None)
    assert sink.log[-4:] == [
        ("enter", SEAL),
        ("call", "get_responses"),
        ("exit", SEAL),
        ("end", SEAL),
    ]


def test_collecting_the_responses_twice_closes_the_unit_once(sink):
    command, instance = _observable(sink, ["responses", "again"])
    command()
    handle = sink.handles[-1]

    assert instance.get_responses() == "responses"
    assert instance.get_responses() == "again"

    assert handle.ends == 1


def test_collecting_too_early_leaves_the_unit_open(sink):
    command, instance = _observable(sink, [NotFinished("still running"), "responses"])
    command()
    handle = sink.handles[-1]

    with pytest.raises(NotFinished):
        instance.get_responses()
    assert handle.ends == 0, "a command that is still running must not be closed"

    assert instance.get_responses() == "responses"
    assert (handle.ends, handle.error_type) == (1, None)


def test_an_execution_that_finished_with_an_error_closes_the_unit_as_failed(sink):
    command, instance = _observable(sink, [ValueError("the tape ran out")])
    command()
    handle = sink.handles[-1]

    with pytest.raises(ValueError):
        instance.get_responses()

    assert (handle.ends, handle.error_type) == (1, "ValueError")
    assert "tape" in (handle.message or "")


def test_a_command_that_could_not_be_started_is_closed_at_once(sink):
    feature, command_def = _feature(sink)
    command = FakeObservable(feature, command_def, log=sink.log, fail=RuntimeError("no route"))

    with pytest.raises(RuntimeError):
        command()

    handle = sink.handles[-1]
    assert (handle.ends, handle.error_type) == (1, "RuntimeError")
    assert close_open() == 0, "nothing was left running"


def test_abandoned_commands_are_closed_as_unsettled(sink):
    command, _instance = _observable(sink, ["responses"])
    command()
    handle = sink.handles[-1]

    assert close_open() == 1
    assert (handle.ends, handle.error_type) == (1, UNSETTLED)
    assert close_open() == 0, "they are closed once"


# -- applying and undoing ---------------------------------------------------------------


def test_instrumenting_twice_measures_each_call_once():
    first, second = RecordingSink(), RecordingSink()
    assert instrument_sila2(first, targets=_targets())
    assert instrument_sila2(second, targets=_targets())

    FakeClient("127.0.0.1", 50052, insecure=True, log=second.log)

    assert len(second.handles) == 1
    assert first.handles == []


def test_undoing_the_instrumentation_leaves_the_calls_alone():
    recording = RecordingSink()
    assert instrument_sila2(recording, targets=_targets())
    uninstrument_sila2()

    log: list[tuple] = []
    client = FakeClient("127.0.0.1", 50052, insecure=True, log=log)
    feature = FakeFeature("SealerControl", client)
    command = FakeUnobservable(feature, FakeCommandDef("Seal"), log=log, result="ok")

    assert command() == "ok"
    assert recording.handles == []
    assert log == [("connecting", "127.0.0.1"), ("call", "unobservable")]


def test_a_broken_sink_cannot_break_the_calls():
    assert instrument_sila2(RecordingSink(broken=True), targets=_targets())

    log: list[tuple] = []
    client = FakeClient("127.0.0.1", 50052, insecure=True, log=log)
    feature = FakeFeature("SealerControl", client)
    command_def = FakeCommandDef("Seal", SEAL_FQI)
    assert FakeUnobservable(feature, command_def, log=log, result="ok")() == "ok"

    instance = FakeInstance("uuid", ["responses"], log)
    assert FakeObservable(feature, command_def, log=log, instance=instance)() is instance
    assert instance.get_responses() == "responses"
    assert close_open() == 0


def test_a_missing_sila2_is_not_worth_a_warning(monkeypatch, recwarn):
    """A script that drives no instruments is ordinary, and one that does fails loudly on its own
    when the library is absent -- so nothing here is a surprise worth reporting."""
    import labcode.sila2_instrument as instrument

    monkeypatch.setattr(instrument, "_sila2_available", lambda: False)
    monkeypatch.setattr(
        instrument, "resolve_targets", lambda: (_ for _ in ()).throw(ImportError("no sila2"))
    )

    assert instrument_sila2(RecordingSink()) is False
    assert list(recwarn) == []


def test_a_sila2_that_moved_is_worth_a_warning(monkeypatch):
    """The library is here but not shaped as expected: nothing else in the output would say so."""
    import labcode.sila2_instrument as instrument

    monkeypatch.setattr(instrument, "_sila2_available", lambda: True)
    monkeypatch.setattr(
        instrument,
        "resolve_targets",
        lambda: (_ for _ in ()).throw(ImportError("no module sila2.client.moved")),
    )

    with pytest.warns(UserWarning, match="records no SiLA2 calls"):
        assert instrument_sila2(RecordingSink()) is False


def test_a_failed_intervention_warns_and_is_rolled_back():
    recording = RecordingSink()
    # `int` cannot be patched, and it is the last target applied -- so the earlier ones must be
    # put back rather than left half-installed.
    with pytest.warns(UserWarning, match="records no SiLA2 calls"):
        assert instrument_sila2(recording, targets=_targets(instance=int)) is False

    log: list[tuple] = []
    FakeClient("127.0.0.1", 50052, insecure=True, log=log)
    assert recording.handles == []
    assert log == [("connecting", "127.0.0.1")]


# -- the real library -------------------------------------------------------------------


def test_the_real_sila2_still_has_the_intervention_points():
    """The intervention points are private attributes of `sila2`; this notices when they move."""
    pytest.importorskip("sila2", reason="the sila2 extra is not installed")

    targets = resolve_targets()

    assert callable(targets.client.__init__)
    assert callable(targets.unobservable.__call__)
    assert callable(targets.observable.__call__)
    assert callable(targets.instance.get_responses)
    assert targets.not_finished is not None
    # What `_command_span` reads off a command definition.
    from sila2.features.silaservice import SiLAServiceFeature

    command = SiLAServiceFeature["GetFeatureDefinition"]
    assert command._identifier == "GetFeatureDefinition"
    assert str(command.fully_qualified_identifier).endswith("/Command/GetFeatureDefinition")
