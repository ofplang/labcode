"""The `Sink` that turns `labcode.sila2_instrument`'s measured units into OpenTelemetry spans.

The instrumentation decides *what* is worth measuring; this decides *how it is recorded*. Swapping
the recording backend means rewriting this file -- and leaving the instrumentation, where the
difficult part lives (which calls to intervene in, how an observable command's real extent is
observed), untouched.

**It configures no provider**, only `trace.get_tracer`. Choosing an exporter and the resource
attributes is the application's job (`labcode.otel` for a run; a UI that drives instruments
directly does it for itself). That is the OpenTelemetry convention, and it has a useful
consequence: with no provider configured the whole thing is a no-op, so a child process that is
not recording needs no flag of its own to check.

Spans are started **without becoming the current span** (`start_span`, not
`start_as_current_span`). An observable command's span stays open until its responses are
collected, and must not adopt the script code that runs meanwhile; wrapping the windows where an
RPC actually runs is the instrumentation's job, through `active`.

**It also records the transport underneath** (`instrument_grpc`), which is what those windows
were for: every gRPC call a SiLA2 client makes becomes a span under the connection or the command
that issued it, so a connection's cost breaks down into the feature definitions it fetched and a
command's into the round trip it really was. That half is OpenTelemetry's own gRPC instrumentation
rather than anything written here -- the only decision this module makes about it is which calls
are worth a span (`_unary_only`).
"""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from labcode.sila2_instrument import KIND_CLIENT

#: Measured-unit kind -> span kind. A call to an instrument is a CLIENT span; left unset it would
#: be INTERNAL, and the record would no longer say that it left this process.
_KINDS = {KIND_CLIENT: SpanKind.CLIENT}

#: Where a failure's kind goes (the OpenTelemetry convention).
ATTR_ERROR_TYPE = "error.type"


class OtelSink:
    """`labcode.sila2_instrument.Sink`, recorded as OpenTelemetry spans."""

    def __init__(self, tracer: Any | None = None) -> None:
        # Fetching the tracer eagerly is safe: the API returns one that does nothing until a
        # provider is configured, and follows the provider once one is.
        self._tracer = tracer if tracer is not None else trace.get_tracer("labcode.sila2")

    def start(self, name: str, *, kind: str, attributes: Mapping[str, Any]) -> Span:
        return self._tracer.start_span(
            name, kind=_KINDS.get(kind, SpanKind.INTERNAL), attributes=dict(attributes)
        )

    def update(self, handle: Span, attributes: Mapping[str, Any]) -> None:
        for key, value in attributes.items():
            handle.set_attribute(key, value)

    def end(
        self, handle: Span, *, error_type: str | None = None, message: str | None = None
    ) -> None:
        if error_type is not None:
            handle.set_attribute(ATTR_ERROR_TYPE, error_type)
            handle.set_status(Status(StatusCode.ERROR, message))
        handle.end()

    def active(self, handle: Span) -> AbstractContextManager[Any]:
        # `end_on_exit=False`: this window only lends the current context. How long the span lives
        # is the instrumentation's to decide -- an observable command opens two windows before it
        # ends.
        return trace.use_span(handle, end_on_exit=False)


# -- the transport underneath -------------------------------------------------------------


#: Whether **this module** wrapped gRPC's channel factories, and so whether unwrapping them is
#: its business. The same rule `labcode.otel.build_provider` follows for a provider: what another
#: program set up is that program's to take down.
_applied_here = False


def _unary_only(client_info: Any) -> bool:
    """Which gRPC calls are recorded: **unary calls yes, server streams no.**

    A SiLA2 observable command opens a server stream for its execution info and `sila2` consumes
    it on a thread of its own, which makes recording that stream wrong in two ways. The span
    would be opened on the first iteration -- on that thread, where nothing is current -- and so
    would land in **a trace of its own** instead of under the command it belongs to, because a
    context is per thread. And intercepting the call replaces the stream with a generator, which
    no longer answers `cancel()`, so `sila2`'s own `cancel_execution_info_subscription` would
    break on it. Letting the call through leaves both alone, and gives up nothing: how long the
    subscription lasted is exactly what the command's own span already measures.

    Nothing is filtered by *name*: the feature definitions a client fetches while it is being
    built are recorded here, under the connection, which is why they are not measured a second
    time as commands (see `labcode.sila2_instrument`).
    """
    return not getattr(client_info, "is_server_stream", False)


def instrument_grpc() -> bool:
    """Have this process's gRPC client calls recorded, and say whether they will be.

    This is the one thing here that reads the **globally configured** provider rather than being
    handed a tracer: OpenTelemetry's gRPC instrumentation takes its provider from the channel it
    wraps, and a `sila2` client builds its own channel. So the application must have configured
    a provider before this is called -- as `labcode.otel.resume_from_env` does -- and with none
    configured the RPC spans are no-ops like everything else here.

    **It must run before any channel exists.** What is wrapped is `grpc.insecure_channel` and
    `grpc.secure_channel`, so a channel built before this returns is never intercepted; a child
    process does it at startup, ahead of the script that opens the clients.

    Returns `False`, and says nothing, when there is no gRPC instrumentation installed (or no
    `grpcio` for it to instrument): the connection and command spans do not depend on it, and an
    interpreter that drives no instruments is right not to carry it. It warns only for the
    surprising case -- the instrumentation is here, and still could not be applied.

    Channel factories somebody else already wrapped are **left as they are** (and reported as
    recording): reconfiguring another program's instrumentation, and later removing it, is not
    this module's to do.
    """
    global _applied_here
    try:
        from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient
    except ImportError:
        return False  # no gRPC instrumentation here, or no gRPC under it
    if _channel_factories_wrapped():
        return True
    try:
        # The client only. A run drives instruments; it serves nothing.
        GrpcInstrumentorClient(filter_=_unary_only).instrument()
    except Exception as exc:
        _warn_not_applied(str(exc))
        return False
    if not _channel_factories_wrapped():
        # `instrument()` reports a dependency it cannot satisfy by logging and returning, so
        # what it did has to be read off the world rather than off its return value.
        _warn_not_applied("gRPC's channel factories were left as they were")
        return False
    _applied_here = True
    return True


def uninstrument_grpc() -> None:
    """Put gRPC's channel factories back, if this module was what wrapped them."""
    global _applied_here
    if not _applied_here:
        return
    _applied_here = False
    with contextlib.suppress(Exception):
        from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

        GrpcInstrumentorClient().uninstrument()


def _channel_factories_wrapped() -> bool:
    """Whether gRPC's channel factories are intercepted -- a fact about the process, which is
    what matters here, rather than about the instrumentor's own bookkeeping."""
    try:
        import grpc
    except ImportError:
        return False
    return all(
        hasattr(getattr(grpc, name, None), "__wrapped__")
        for name in ("insecure_channel", "secure_channel")
    )


def _warn_not_applied(reason: str) -> None:
    warnings.warn(
        f"labcode: cannot instrument gRPC, so this run records no RPCs under its SiLA2 "
        f"calls: {reason}",
        stacklevel=3,
    )
