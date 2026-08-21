"""Recording a labcode run as OpenTelemetry traces: the implementation behind `labcode.record`.

This is the half that knows OpenTelemetry. It sets up the provider, records the run and its
operations, hands a child process what it needs to join the same trace, and -- in that child --
resumes from it and instruments the SiLA2 client (`labcode.sila2_instrument` via
`labcode.otel_sila2`). Replacing it means writing one more module like this one and adding two
lines to `labcode.record`; see that module's docstring.

**Configuration is OpenTelemetry's own.** The endpoint, headers, timeouts, resource attributes
and sampling all come from the standard ``OTEL_*`` environment variables, so an operator
configures this the way they would configure anything else that speaks OTLP, and labcode adds no
settings of its own. Two small exceptions, both deliberate:

* ``service.name`` defaults to ``labcode`` when ``OTEL_SERVICE_NAME`` says nothing (otherwise the
  record is attributed to ``unknown_service``). An explicit value always wins.
* ``LC_TRACE_FILE`` writes the spans to a file as well, for looking at a record without standing
  up a collector. It *adds* an exporter; the standard ``OTEL_TRACES_EXPORTER=none`` is what turns
  the OTLP one off.

Notably absent: any timeout of labcode's own. If a collector is configured that does not answer,
the export waits as long as OpenTelemetry's default says, and a child process's exit -- and so the
completion of its operation -- waits with it. That cost is known and accepted; the knob is
``OTEL_EXPORTER_OTLP_TIMEOUT``, and it belongs to whoever pointed the run at that collector.

**Spans are batched, in the parent and in the child alike.** A child is short-lived, so its
batching delay is small and it flushes on the way out -- but it is still batched, because
exporting synchronously would put the collector's latency inside the operation and let the act of
measuring change what is measured. What that costs is bounded: a child killed outright (an
operation that ran past ``op_timeout``) loses at most the spans of the last half second, and the
span of whatever command it was waiting on was never going to be exported anyway -- an unfinished
span has not been handed to an exporter at all.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

from opentelemetry import context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span, Status, StatusCode

from labcode.otel_sila2 import ATTR_ERROR_TYPE, OtelSink
from labcode.record import ATTR_MISSION, RUN_STOPPED, SPAN_RUN
from labcode.sila2_instrument import instrument_sila2, uninstrument_sila2

#: How the parent tells a child which record to join (the W3C names, in the conventional
#: environment-variable spelling). Nothing outside this module knows them.
TRACEPARENT = "TRACEPARENT"
TRACESTATE = "TRACESTATE"

#: Writing the spans to a file as well, one JSON object per line. The **process id is added to
#: the name** (``run.jsonl`` -> ``run.1234.jsonl``): a run and its children all record, and
#: appending from several processes to one file would interleave half-written lines.
TRACE_FILE_ENV = "LC_TRACE_FILE"

#: The standard way to turn the OTLP exporter off, honoured here because this module -- rather
#: than the SDK's own autoconfiguration -- is what builds the exporter.
EXPORTER_ENV = "OTEL_TRACES_EXPORTER"

SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
DEFAULT_SERVICE_NAME = "labcode"

#: How long a child holds a finished span before exporting it, in milliseconds. Short, because
#: the process may be killed; not zero, because exporting must not happen on the script's thread.
CHILD_SCHEDULE_DELAY_MILLIS = 500


# -- provider ----------------------------------------------------------------------------


def _resource() -> Resource:
    """The resource for this process: whatever ``OTEL_RESOURCE_ATTRIBUTES`` says, with a name
    filled in only if nobody supplied one."""
    if os.environ.get(SERVICE_NAME_ENV):
        return Resource.create({})
    return Resource.create({"service.name": DEFAULT_SERVICE_NAME})


def _exporters() -> list[SpanExporter]:
    """The exporters this process should use, per the environment.

    The OTLP exporter is imported here rather than at module scope: recording can be exercised
    (and tested) with only the SDK installed, and a missing exporter package should not stop a
    run that is recording to a file."""
    exporters: list[SpanExporter] = []
    if (os.environ.get(EXPORTER_ENV) or "").strip().lower() != "none":
        # A missing exporter package is not fatal: the file exporter below may be all that was
        # wanted, and a run must not stop over how its record is shipped.
        with contextlib.suppress(Exception):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporters.append(OTLPSpanExporter())
    path = os.environ.get(TRACE_FILE_ENV)
    if path:
        exporters.append(JsonLinesExporter(path))
    return exporters


def build_provider(*, child: bool = False) -> tuple[TracerProvider, bool]:
    """``(provider, owned)`` for this process.

    An SDK provider already installed is **reused and not owned**: the global provider can be set
    only once per process, and shutting down one this module did not create would silence whoever
    did create it (a library embedding `labcode.runner` in a longer-lived program)."""
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return current, False
    provider = TracerProvider(resource=_resource())
    delay = CHILD_SCHEDULE_DELAY_MILLIS if child else None
    for exporter in _exporters():
        provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=delay))
    trace.set_tracer_provider(provider)
    return provider, True


class JsonLinesExporter(SpanExporter):
    """Appends one JSON object per span to ``LC_TRACE_FILE``.

    For reading a record without a collector -- and for a test to check that a child's spans
    really landed in its parent's trace, which nothing inside one process can show."""

    def __init__(self, path: str) -> None:
        root, extension = os.path.splitext(path)
        self._path = f"{root}.{os.getpid()}{extension}"

    @property
    def path(self) -> str:
        """Where this process actually writes (the name it was given, plus its process id)."""
        return self._path

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            with open(self._path, "a", encoding="utf-8") as stream:
                for span in spans:
                    stream.write(json.dumps(_as_json(span), default=str) + "\n")
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        return None


def _as_json(span: ReadableSpan) -> dict[str, Any]:
    # A span always has a context by the time it is exported; the type says otherwise, and a
    # record with a hole in it is worth more than an exporter that raises.
    span_context = span.get_span_context()
    return {
        "name": span.name,
        "trace_id": f"{span_context.trace_id:032x}" if span_context else None,
        "span_id": f"{span_context.span_id:016x}" if span_context else None,
        "parent_span_id": (f"{span.parent.span_id:016x}" if span.parent is not None else None),
        "kind": span.kind.name,
        "service": (span.resource.attributes or {}).get("service.name"),
        "start": span.start_time,
        "end": span.end_time,
        "status": span.status.status_code.name,
        "message": span.status.description,
        "attributes": dict(span.attributes or {}),
    }


# -- the run and its operations -----------------------------------------------------------


class OtelRecorder:
    """`labcode.record.Recorder`, recorded as OpenTelemetry spans.

    One run is one trace: its root span is the run, and every operation is a span under it --
    **parented explicitly, never by being the current span**, because operations overlap and the
    loop that dispatches them interleaves. The only stretch where an operation is made current is
    dispatch itself (`op_active`), which is how the child process it starts finds it.
    """

    def __init__(self, *, provider: TracerProvider | None = None) -> None:
        if provider is not None:
            self._provider, self._owned = provider, False
        else:
            self._provider, self._owned = build_provider()
        self._tracer = self._provider.get_tracer("labcode.otel")
        self._root: Span | None = None
        self._ops: dict[str, Span] = {}

    def run_started(self, *, mission_id: str | None = None) -> str | None:
        attributes = {ATTR_MISSION: mission_id} if mission_id else {}
        self._root = self._tracer.start_span(SPAN_RUN, attributes=attributes)
        trace_id = self._root.get_span_context().trace_id
        return f"{trace_id:032x}" if trace_id else None

    def run_finished(self, *, error_type: str | None = None, message: str | None = None) -> None:
        # An operation still open here was never observed finishing -- the run stopped first.
        for uuid in list(self._ops):
            self.op_finished(uuid, error_type=RUN_STOPPED, message="the run ended first")
        root, self._root = self._root, None
        if root is not None:
            _finish(root, error_type, message)

    def op_started(self, uuid: str, name: str, attributes: Mapping[str, Any]) -> None:
        parent = trace.set_span_in_context(self._root) if self._root is not None else None
        self._ops[uuid] = self._tracer.start_span(name, context=parent, attributes=dict(attributes))

    def op_active(self, uuid: str) -> AbstractContextManager[Any]:
        span = self._ops.get(uuid)
        if span is None:
            return nullcontext()
        return trace.use_span(span, end_on_exit=False)

    def op_finished(
        self,
        uuid: str,
        *,
        error_type: str | None = None,
        message: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        span = self._ops.pop(uuid, None)
        if span is None:
            return
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        _finish(span, error_type, message)

    def child_env(self) -> Mapping[str, str] | None:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        traceparent = carrier.get("traceparent")
        if not traceparent:
            return None
        env = {TRACEPARENT: traceparent}
        tracestate = carrier.get("tracestate")
        if tracestate:
            env[TRACESTATE] = tracestate
        return env

    def shutdown(self) -> None:
        self.run_finished()  # a run that ended without saying so still gets a closed record
        if self._owned:
            self._provider.shutdown()


def _finish(span: Span, error_type: str | None, message: str | None) -> None:
    if error_type is not None:
        span.set_attribute(ATTR_ERROR_TYPE, error_type)
        span.set_status(Status(StatusCode.ERROR, message))
    span.end()


# -- the child side -----------------------------------------------------------------------


@dataclass
class ChildSession:
    """What a recording child process has to undo on the way out."""

    provider: TracerProvider
    owned: bool
    #: What `context.attach` handed back, to be given to `context.detach` unexamined.
    token: Any

    def finish(self) -> None:
        uninstrument_sila2()
        context.detach(self.token)
        if self.owned:
            self.provider.shutdown()  # flushes what has not been exported yet


def resume_from_env(
    env: Mapping[str, str] | None = None, *, provider: TracerProvider | None = None
) -> ChildSession | None:
    """Join the record the parent process started, and instrument this process's SiLA2 calls.

    Returns `None` when the environment carries no record to join -- which is what a run that is
    not recording looks like from in here, and needs no flag of its own."""
    environment = os.environ if env is None else env
    traceparent = environment.get(TRACEPARENT)
    if not traceparent:
        return None

    resolved, owned = (provider, False) if provider is not None else build_provider(child=True)
    carrier = {"traceparent": traceparent}
    tracestate = environment.get(TRACESTATE)
    if tracestate:
        carrier["tracestate"] = tracestate
    token = context.attach(propagate.extract(carrier))
    instrument_sila2(OtelSink(tracer=resolved.get_tracer("labcode.sila2")))
    return ChildSession(provider=resolved, owned=owned, token=token)
