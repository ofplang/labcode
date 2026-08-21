"""Tests for the OpenTelemetry `Sink` (`labcode.otel_sila2`).

What is checked here is the *mapping* -- a measured unit becomes a CLIENT span, a failure becomes
an error status the record can be filtered on, and `active` lends the current context **without
ending the span**. The semantics of when units begin and end is `test_sila2_instrument.py`'s.

Three of these are the regression guard for the rule that makes gRPC instrumentation possible
later: a unit is *not* current merely because it started (an observable command's span would
otherwise adopt the script code that runs while the instrument works), it *is* current inside the
window where an RPC runs, and both of an observable command's windows put their RPC under the same
command.

Skipped unless the OpenTelemetry SDK is installed. The tracer is passed in rather than taken from
the global provider, which a test must not set (it is process-wide and can be set only once).
"""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.sdk", reason="the otel extra is not installed")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode  # noqa: E402

from labcode.otel_sila2 import ATTR_ERROR_TYPE, OtelSink  # noqa: E402
from labcode.sila2_instrument import (  # noqa: E402
    KIND_CLIENT,
    Targets,
    instrument_sila2,
    uninstrument_sila2,
)

SEAL = "sila2 SealerControl.Seal"


@pytest.fixture
def recorded():
    """``(sink, exporter, tracer)`` over an in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return OtelSink(tracer=tracer), exporter, tracer


@pytest.fixture(autouse=True)
def _undo_instrumentation():
    yield
    uninstrument_sila2()


def test_a_measured_unit_becomes_a_client_span(recorded):
    sink, exporter, _tracer = recorded

    handle = sink.start(SEAL, kind=KIND_CLIENT, attributes={"sila.command": "Seal"})
    sink.update(handle, {"sila.execution_uuid": "3f2b8c1a"})
    sink.end(handle)

    (span,) = exporter.get_finished_spans()
    assert span.name == SEAL
    assert span.kind is SpanKind.CLIENT, "a call to an instrument leaves this process"
    assert span.attributes["sila.command"] == "Seal"
    assert span.attributes["sila.execution_uuid"] == "3f2b8c1a"
    assert span.status.status_code is not StatusCode.ERROR


def test_a_failure_is_recorded_as_an_error_with_its_kind(recorded):
    sink, exporter, _tracer = recorded

    handle = sink.start(SEAL, kind=KIND_CLIENT, attributes={})
    sink.end(handle, error_type="sila2_command_unsettled", message="never collected")

    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "never collected"
    assert span.attributes[ATTR_ERROR_TYPE] == "sila2_command_unsettled"


def test_starting_a_unit_does_not_make_it_current(recorded):
    sink, exporter, tracer = recorded

    handle = sink.start(SEAL, kind=KIND_CLIENT, attributes={})
    # Stands in for the script code that runs while the instrument works.
    with tracer.start_as_current_span("meanwhile") as meanwhile:
        pass
    sink.end(handle)

    assert meanwhile.parent is None, "an open unit must not adopt what merely follows it"
    assert exporter.get_finished_spans()[1].parent is None


def test_the_window_lends_the_current_context_without_ending_the_unit(recorded):
    sink, exporter, tracer = recorded

    handle = sink.start(SEAL, kind=KIND_CLIENT, attributes={})
    with sink.active(handle):
        tracer.start_span("rpc").end()  # stands in for the gRPC span of the wrapped RPC
    assert handle.is_recording(), "the window must not end the unit"

    sink.end(handle)
    rpc, command = exporter.get_finished_spans()
    assert [rpc.name, command.name] == ["rpc", SEAL]
    assert rpc.parent is not None
    assert rpc.parent.span_id == command.context.span_id


def test_both_windows_of_an_observable_command_put_their_rpc_under_it(recorded):
    """The whole point of the two-window rule: initiation and result collection land under the
    same command, while what happens between them does not."""
    sink, exporter, tracer = recorded

    class Instance:
        execution_uuid = "3f2b8c1a"

        def get_responses(self):
            tracer.start_span("rpc result").end()
            return "responses"

    instance = Instance()

    class Feature:
        _identifier = "SealerControl"
        _parent_client = None

    class CommandDef:
        _identifier = "Seal"

    class Command:
        _parent_feature = Feature()
        _wrapped_command = CommandDef()

        def __call__(self):
            tracer.start_span("rpc start").end()
            return instance

    class Unused:
        def __init__(self) -> None: ...
        def __call__(self) -> None: ...

    assert instrument_sila2(
        sink,
        targets=Targets(
            client=Unused,
            unobservable=Unused,
            observable=Command,
            instance=Instance,
            not_finished=None,
        ),
    )

    assert Command()() is instance
    tracer.start_span("meanwhile").end()  # the script's own work, between the two windows
    assert instance.get_responses() == "responses"

    spans = {span.name: span for span in exporter.get_finished_spans()}
    command_id = spans[SEAL].context.span_id
    assert spans["rpc start"].parent.span_id == command_id
    assert spans["rpc result"].parent.span_id == command_id
    assert spans["meanwhile"].parent is None, "only the RPC windows are inside the command"
    assert spans[SEAL].attributes["sila.execution_uuid"] == "3f2b8c1a"
