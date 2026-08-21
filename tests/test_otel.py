"""Tests for the OpenTelemetry recorder (`labcode.otel`).

Two things matter here beyond the plain mapping of a run and its operations onto spans:

* **operations do not nest by accident.** They overlap, and the loop that dispatches them
  interleaves, so an operation is a child of the run and of nothing else -- being started while
  another one is open must not make it that one's child.
* **a child process joins the same trace.** The whole record depends on it, and no assertion
  inside one process can show it end to end -- but this comes close: the environment the parent
  would hand a child is fed straight back into `resume_from_env`, and the span that follows must
  land under the operation that dispatched it.

Skipped unless the OpenTelemetry SDK is installed. Every test passes its own provider: the global
one can be set only once per process, so a test that installed one would leak into the next.
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("opentelemetry.sdk", reason="the otel extra is not installed")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode  # noqa: E402

from labcode.otel import (  # noqa: E402
    TRACEPARENT,
    JsonLinesExporter,
    OtelRecorder,
    resume_from_env,
)
from labcode.otel_sila2 import ATTR_ERROR_TYPE  # noqa: E402
from labcode.record import ATTR_MISSION, ATTR_NODE, RUN_STOPPED, SPAN_RUN  # noqa: E402

SEAL = "process Seal"


@pytest.fixture
def recorded():
    """``(recorder, exporter, provider)`` over an in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return OtelRecorder(provider=provider), exporter, provider


def _by_name(exporter):
    return {span.name: span for span in exporter.get_finished_spans()}


def test_a_run_is_the_root_of_its_operations(recorded):
    recorder, exporter, _provider = recorded

    trace_id = recorder.run_started(mission_id="M-2026-001")
    recorder.op_started("op-1", SEAL, {ATTR_NODE: "seal#0"})
    recorder.op_finished("op-1")
    recorder.run_finished()

    assert trace_id is not None and len(trace_id) == 32
    spans = _by_name(exporter)
    run, operation = spans[SPAN_RUN], spans[SEAL]
    assert run.parent is None
    assert run.attributes[ATTR_MISSION] == "M-2026-001"
    assert operation.parent.span_id == run.context.span_id
    assert operation.attributes[ATTR_NODE] == "seal#0"
    assert f"{run.context.trace_id:032x}" == trace_id


def test_a_run_without_a_mission_records_no_mission(recorded):
    recorder, exporter, _provider = recorded

    recorder.run_started()
    recorder.run_finished()

    assert ATTR_MISSION not in _by_name(exporter)[SPAN_RUN].attributes


def test_overlapping_operations_are_siblings(recorded):
    recorder, exporter, _provider = recorded

    recorder.run_started()
    recorder.op_started("op-1", SEAL, {})
    recorder.op_started("op-2", "transport", {})  # dispatched while the first is still running
    recorder.op_finished("op-2")
    recorder.op_finished("op-1")
    recorder.run_finished()

    spans = _by_name(exporter)
    run = spans[SPAN_RUN]
    assert spans[SEAL].parent.span_id == run.context.span_id
    assert spans["transport"].parent.span_id == run.context.span_id


def test_a_failed_operation_is_recorded_with_its_reason(recorded):
    recorder, exporter, _provider = recorded

    recorder.run_started()
    recorder.op_started("op-1", SEAL, {})
    recorder.op_finished("op-1", error_type="op_timeout", message="did not finish within 7200s")
    recorder.run_finished(error_type="op_timeout", message="the run stopped")

    spans = _by_name(exporter)
    assert spans[SEAL].status.status_code is StatusCode.ERROR
    assert spans[SEAL].attributes[ATTR_ERROR_TYPE] == "op_timeout"
    assert "7200" in spans[SEAL].status.description
    assert spans[SPAN_RUN].status.status_code is StatusCode.ERROR


def test_operations_still_open_when_the_run_ends_say_so(recorded):
    recorder, exporter, _provider = recorded

    recorder.run_started()
    recorder.op_started("op-1", SEAL, {})
    recorder.run_finished()

    operation = _by_name(exporter)[SEAL]
    assert operation.attributes[ATTR_ERROR_TYPE] == RUN_STOPPED
    assert operation.status.status_code is StatusCode.ERROR


def test_shutting_down_closes_a_record_nobody_closed(recorded):
    recorder, exporter, _provider = recorded

    recorder.run_started()
    recorder.op_started("op-1", SEAL, {})
    recorder.shutdown()  # a provider it does not own is left alone

    assert set(_by_name(exporter)) == {SPAN_RUN, SEAL}


# -- what a child process is told --------------------------------------------------------


def test_nothing_is_handed_to_a_child_outside_a_dispatch(recorded):
    recorder, _exporter, _provider = recorded
    recorder.run_started()
    recorder.op_started("op-1", SEAL, {})

    assert recorder.child_env() is None, "only dispatch ties a child to an operation"


def test_a_childs_spans_land_under_the_operation_that_dispatched_it(recorded):
    recorder, exporter, provider = recorded
    recorder.run_started()
    recorder.op_started("op-1", SEAL, {})

    with recorder.op_active("op-1"):
        env = recorder.child_env()  # what `spawn` would put in the child's environment

    assert env is not None and TRACEPARENT in env

    # Now be the child: resume from that environment and record something.
    session = resume_from_env(env, provider=provider)
    assert session is not None
    try:
        provider.get_tracer("child").start_span("sila2 SealerControl.Seal").end()
    finally:
        session.finish()

    recorder.op_finished("op-1")
    recorder.run_finished()

    spans = _by_name(exporter)
    operation, command = spans[SEAL], spans["sila2 SealerControl.Seal"]
    assert command.context.trace_id == operation.context.trace_id, "one run is one trace"
    assert command.parent.span_id == operation.context.span_id


def test_a_child_with_nothing_in_its_environment_does_not_record():
    assert resume_from_env({}) is None


# -- the file exporter -------------------------------------------------------------------


def test_the_file_exporter_writes_one_line_per_span_and_names_the_process(tmp_path, recorded):
    recorder, _exporter, _provider = recorded
    target = tmp_path / "run.jsonl"
    exporter = JsonLinesExporter(str(target))

    assert exporter.path == str(tmp_path / f"run.{os.getpid()}.jsonl")

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    recorder = OtelRecorder(provider=provider)
    recorder.run_started(mission_id="M-2026-001")
    recorder.op_started("op-1", SEAL, {ATTR_NODE: "seal#0"})
    recorder.op_finished("op-1")
    recorder.run_finished()

    with open(exporter.path, encoding="utf-8") as stream:
        lines = [json.loads(line) for line in stream]
    assert [line["name"] for line in lines] == [SEAL, SPAN_RUN]
    assert lines[0]["attributes"][ATTR_NODE] == "seal#0"
    assert lines[0]["parent_span_id"] == lines[1]["span_id"]
    assert lines[1]["parent_span_id"] is None
