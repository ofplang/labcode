"""The record crosses the process boundary (`labcode._child` + `labcode.otel`).

Every other test can only show one half of this: the parent hands a child something, or a
child resumes from something it was handed. What nothing inside one process can show is that
they meet -- that what a real child process records lands under the operation that really
dispatched it, in the same trace. So this drives a real run whose script records a span of
its own, and reads the record back off disk.

`LC_TRACE_FILE` is what makes that readable without a collector, and it is why the exporter
writes one file per process: the parent and its children all record, and appending to one
file from several processes would interleave a line sooner or later.

Skipped unless the OpenTelemetry SDK is installed -- in the interpreter running the test
*and* in the one running the script, which are the same one (`sys.executable`).

**This is the test that installs a global tracer provider** (a real `trace=True` run does).
A provider can be set only once per process, so another test that records for real would
share this one's exporter; there is only this one.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk", reason="the otel extra is not installed")

from labcode.record import SPAN_RUN, SPAN_TRANSPORT  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
TWF = str(FIX / "transport.workflow.yaml")
TENV = FIX / "transport.env.yaml"

#: What the transport script records, standing in for the SiLA2 command a real one would
#: issue (this interpreter has no `sila2`, and the point here is the process boundary).
CHILD_SPAN = "the child was here"
CHILD_SCRIPT = (
    "from opentelemetry import trace; "
    f"trace.get_tracer('child').start_span({CHILD_SPAN!r}).end()"
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _spans(directory: Path) -> list[dict]:
    """Every span recorded under `directory`, from all of the processes that wrote one."""
    spans: list[dict] = []
    for path in sorted(glob.glob(str(directory / "record.*.jsonl"))):
        with open(path, encoding="utf-8") as stream:
            spans.extend(json.loads(line) for line in stream)
    return spans


def test_a_childs_record_joins_the_operation_that_dispatched_it(tmp_path, monkeypatch):
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    environment = tmp_path / "env.yaml"
    environment.write_text(
        TENV.read_text(encoding="utf-8").replace(
            "moved = (view, from_spot, to_spot, transporter)", CHILD_SCRIPT
        ),
        encoding="utf-8",
    )
    # Read off disk, so no collector is needed; the child inherits both of these.
    monkeypatch.setenv("LC_TRACE_FILE", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")

    clock = FakeClock()
    runner = LabcodeRunner(
        TWF, str(environment), seconds_per_tick=0.001, monotonic=clock.monotonic,
        sleep=clock.sleep, random_seed=0, running_task_margin=1, trace=True,
        mission_id="M-2026-001",
    )
    try:
        runner.run()
    finally:
        runner.sim.close()
    assert not runner.failed

    spans = _spans(tmp_path)
    by_name = {span["name"]: span for span in spans}
    assert SPAN_RUN in by_name, f"the run was not recorded: {[s['name'] for s in spans]}"
    assert CHILD_SPAN in by_name, "the child recorded nothing"

    run, move, child = by_name[SPAN_RUN], by_name[SPAN_TRANSPORT], by_name[CHILD_SPAN]
    assert run["attributes"]["mission.id"] == "M-2026-001"
    assert move["parent_span_id"] == run["span_id"]
    # The point of the whole chain: a different process, the same trace, under the operation
    # that dispatched it.
    assert child["trace_id"] == move["trace_id"] == runner.trace_id
    assert child["parent_span_id"] == move["span_id"]
    assert child["service"] == move["service"] == "labcode"


def test_a_run_that_is_not_recording_writes_nothing(tmp_path, monkeypatch):
    """The default. The child is the same child, and it records nothing because nothing in
    its environment says to."""
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    monkeypatch.setenv("LC_TRACE_FILE", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")

    clock = FakeClock()
    runner = LabcodeRunner(
        TWF, str(TENV), seconds_per_tick=0.001, monotonic=clock.monotonic,
        sleep=clock.sleep, random_seed=0, running_task_margin=1,
    )
    try:
        runner.run()
    finally:
        runner.sim.close()

    assert not runner.failed
    assert runner.trace_id is None
    assert _spans(tmp_path) == []


def test_a_recording_child_gets_the_protobuf_implementation_sila2_needs(tmp_path, monkeypatch):
    """The other half of what a child is handed, and the one nothing in this interpreter can
    show: `sila2` cannot open a client in a process whose protobuf is the C implementation, and
    it can only ask for the pure-Python one *before* protobuf is imported -- which recording,
    by importing an exporter that speaks protobuf, is exactly what takes away. The child is told
    in its environment instead, where no import order can reach it.
    """
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    pytest.importorskip("google.protobuf", reason="protobuf is not installed")
    from labcode.runner import LabcodeRunner

    # The script reports what its process actually resolved, as a span name.
    environment = tmp_path / "env.yaml"
    environment.write_text(
        TENV.read_text(encoding="utf-8").replace(
            "moved = (view, from_spot, to_spot, transporter)",
            "from google.protobuf.internal import api_implementation; "
            "from opentelemetry import trace; "
            "trace.get_tracer('child').start_span('protobuf ' + api_implementation.Type()).end()",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LC_TRACE_FILE", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")

    clock = FakeClock()
    runner = LabcodeRunner(
        TWF, str(environment), seconds_per_tick=0.001, monotonic=clock.monotonic,
        sleep=clock.sleep, random_seed=0, running_task_margin=1, trace=True,
    )
    try:
        runner.run()
    finally:
        runner.sim.close()
    assert not runner.failed

    names = [span["name"] for span in _spans(tmp_path)]
    assert "protobuf python" in names, f"the child resolved something else: {names}"


def test_a_recording_child_has_its_grpc_calls_instrumented(tmp_path, monkeypatch):
    """The RPCs underneath a SiLA2 call are recorded in the child, and the wrapping has to be in
    place before the script opens a client -- so what this checks is that the *startup* did it,
    not that some later call did. The child reports what it found, as a span name."""
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    pytest.importorskip("grpc", reason="grpcio is not installed")
    pytest.importorskip(
        "opentelemetry.instrumentation.grpc", reason="the gRPC instrumentation is not installed"
    )
    from labcode.runner import LabcodeRunner

    environment = tmp_path / "env.yaml"
    environment.write_text(
        TENV.read_text(encoding="utf-8").replace(
            "moved = (view, from_spot, to_spot, transporter)",
            "import grpc; "
            "from opentelemetry import trace; "
            "state = 'wrapped' if hasattr(grpc.insecure_channel, '__wrapped__') else 'plain'; "
            "trace.get_tracer('child').start_span('grpc ' + state).end()",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LC_TRACE_FILE", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")

    clock = FakeClock()
    runner = LabcodeRunner(
        TWF, str(environment), seconds_per_tick=0.001, monotonic=clock.monotonic,
        sleep=clock.sleep, random_seed=0, running_task_margin=1, trace=True,
    )
    try:
        runner.run()
    finally:
        runner.sim.close()
    assert not runner.failed

    names = [span["name"] for span in _spans(tmp_path)]
    assert "grpc wrapped" in names, f"the child's gRPC calls are not recorded: {names}"
