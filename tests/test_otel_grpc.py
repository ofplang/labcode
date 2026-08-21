"""Recording the gRPC calls underneath a SiLA2 call (`labcode.otel_sila2`).

The spans themselves are OpenTelemetry's own gRPC instrumentation, and are not this project's to
test. What *is* this project's is the one decision it makes about them -- which calls are worth a
span -- and the two mechanical facts that decision rests on: that a unary call is recorded under
whatever is current, and that a **server-streaming** call comes back exactly as it went in.

That second one is the point. A SiLA2 observable command subscribes to its execution info over a
server stream that `sila2` reads on a thread of its own, so a span for it would open where nothing
is current and land in a trace of its own -- and the interception would hand `sila2` a generator
in place of a stream it expects to be able to cancel. Both are avoided by not intercepting it, and
this asserts that the object handed back is the very one the call produced.

Skipped unless the gRPC instrumentation is installed, with the `grpcio` it intercepts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk", reason="the otel extra is not installed")
pytest.importorskip(
    "opentelemetry.instrumentation.grpc", reason="the gRPC instrumentation is not installed"
)

import grpc  # noqa: E402
from opentelemetry.instrumentation.grpc import client_interceptor  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind  # noqa: E402

from labcode.otel_sila2 import (  # noqa: E402
    _unary_only,
    instrument_grpc,
    uninstrument_grpc,
)

#: A real SiLA2 method path: the feature definitions a client fetches while it is being built.
#: They are deliberately *not* filtered out -- recorded here, under the connection, they are the
#: reason `labcode.sila2_instrument` does not measure them a second time as commands.
FETCH = "/sila2.org.silastandard.core.silaservice.v1.SiLAService/GetFeatureDefinition"

#: What an observable command's execution-info subscription looks like to a filter.
INFO_STREAM = "/sila2.org.silastandard.examples.sealer.v1.SealerControl/Seal_Info"


@pytest.fixture
def recorded():
    """``(interceptor, exporter)`` recording into memory, filtered as labcode filters."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    interceptor = client_interceptor(tracer_provider=provider, filter_=_unary_only)
    return interceptor, exporter


def _unary_call(full_method: str = FETCH) -> SimpleNamespace:
    return SimpleNamespace(full_method=full_method, timeout=None)


def _stream_call(full_method: str = INFO_STREAM, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        full_method=full_method,
        is_client_stream=kwargs.get("is_client_stream", False),
        is_server_stream=kwargs.get("is_server_stream", True),
        timeout=None,
    )


def test_a_unary_call_is_recorded_as_a_span_named_by_its_method(recorded):
    interceptor, exporter = recorded

    result = interceptor.intercept_unary("request", (), _unary_call(), lambda r, m: "response")

    assert result == "response"
    (span,) = exporter.get_finished_spans()
    # The name is the method path, which is how a fetch of a feature definition can be told
    # from the command that follows it without either one being named twice.
    assert span.name == FETCH
    assert span.kind is SpanKind.CLIENT
    assert span.attributes["rpc.system"] == "grpc"
    assert span.attributes["rpc.method"] == "GetFeatureDefinition"
    assert span.attributes["rpc.service"] == FETCH.lstrip("/").split("/")[0]


def test_a_recorded_call_carries_the_trace_context_to_the_instrument(recorded):
    """A recorded RPC sends the current trace context in its gRPC metadata -- a fact worth
    asserting because it is the one thing recording changes on the wire. A SiLA2 server ignores
    metadata that is not a SiLA Client Metadata key."""
    interceptor, _exporter = recorded
    seen: dict[str, str] = {}

    def invoker(request, metadata):
        seen.update(dict(metadata))
        return "response"

    interceptor.intercept_unary("request", (), _unary_call(), invoker)

    assert "traceparent" in seen, f"nothing was propagated: {seen}"


def test_a_server_stream_is_handed_back_exactly_as_it_came(recorded):
    interceptor, exporter = recorded
    stream = object()  # stands in for the rendezvous `sila2` keeps, and cancels

    result = interceptor.intercept_stream("request", (), _stream_call(), lambda r, m: stream)

    assert result is stream, "the stream was wrapped, so it no longer answers cancel()"
    assert exporter.get_finished_spans() == ()


def test_only_a_server_stream_is_left_out():
    """The filter reads the one fact that distinguishes the subscription: a unary call has no
    such attribute at all, which is why the question is asked with a default."""
    assert _unary_only(_unary_call()) is True
    assert _unary_only(_stream_call(is_server_stream=False, is_client_stream=True)) is True
    assert _unary_only(_stream_call()) is False


def test_instrumenting_wraps_the_channel_factories_and_undoes_it():
    plain = (grpc.insecure_channel, grpc.secure_channel)
    try:
        assert instrument_grpc() is True
        # A channel can only be intercepted if the factory it comes from is, which is why this
        # has to happen before any client exists.
        assert grpc.insecure_channel is not plain[0]
        assert hasattr(grpc.insecure_channel, "__wrapped__")
        assert hasattr(grpc.secure_channel, "__wrapped__")
    finally:
        uninstrument_grpc()
    assert (grpc.insecure_channel, grpc.secure_channel) == plain


def test_factories_somebody_else_wrapped_are_left_alone(monkeypatch):
    """Reporting that RPCs are recorded, and not touching them: another program's
    instrumentation is its own to configure and its own to remove."""

    class AlreadyWrapped:
        __wrapped__ = None

    theirs = AlreadyWrapped()
    monkeypatch.setattr(grpc, "insecure_channel", theirs)
    monkeypatch.setattr(grpc, "secure_channel", theirs)

    assert instrument_grpc() is True
    uninstrument_grpc()
    assert grpc.insecure_channel is theirs
    assert grpc.secure_channel is theirs
