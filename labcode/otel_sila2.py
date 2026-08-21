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
"""

from __future__ import annotations

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
