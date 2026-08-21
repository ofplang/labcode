"""Tests for the recording seam (`labcode.record`).

The seam is what the execution path sees, so what is checked here is the two properties the
execution path relies on: **a run that is not recording pays nothing and behaves identically**,
and **no failure inside a recorder reaches the caller**. Neither needs a recording backend
installed, which is the point -- these run wherever the tests run.
"""

from __future__ import annotations

import sys

import pytest

from labcode import record
from labcode.record import NullRecorder, build_recorder, child_recording


def test_a_run_that_is_not_recording_gets_a_recorder_that_does_nothing():
    recorder = build_recorder(enabled=False)

    assert isinstance(recorder, NullRecorder)
    assert recorder.run_started(mission_id="M-2026-001") is None
    recorder.op_started("op-1", "process Seal", {"ofp.node": "seal#0"})
    with recorder.op_active("op-1"):
        assert recorder.child_env() is None
    recorder.op_finished("op-1")
    recorder.run_finished()
    recorder.shutdown()


def test_asking_to_record_without_the_extra_warns_and_runs_unrecorded(monkeypatch):
    monkeypatch.setitem(sys.modules, "labcode.otel", None)

    with pytest.warns(UserWarning, match="cannot record this run"):
        recorder = build_recorder(enabled=True)

    assert isinstance(recorder, NullRecorder)


class Broken:
    """A recorder in which everything fails."""

    def run_started(self, *, mission_id=None):
        raise RuntimeError("broken")

    def run_finished(self, *, error_type=None, message=None):
        raise RuntimeError("broken")

    def op_started(self, uuid, name, attributes):
        raise RuntimeError("broken")

    def op_active(self, uuid):
        raise RuntimeError("broken")

    def op_finished(self, uuid, *, error_type=None, message=None):
        raise RuntimeError("broken")

    def child_env(self):
        raise RuntimeError("broken")

    def shutdown(self):
        raise RuntimeError("broken")


class BrokenWindow:
    """A recorder whose window fails on the way in and out, rather than when it is asked for."""

    def op_active(self, uuid):
        return self

    def __enter__(self):
        raise RuntimeError("broken")

    def __exit__(self, *exc):
        raise RuntimeError("broken")


def test_no_failure_inside_a_recorder_reaches_the_caller(monkeypatch):
    monkeypatch.setattr(record, "OtelRecorder", Broken, raising=False)
    guarded = record._Guarded(Broken())

    assert guarded.run_started(mission_id="M-1") is None
    guarded.op_started("op-1", "process Seal", {})
    with guarded.op_active("op-1"):
        assert guarded.child_env() is None
    guarded.op_finished("op-1", error_type="op_timeout", message="took too long")
    guarded.run_finished(error_type="op_timeout")
    guarded.shutdown()


def test_a_window_that_cannot_be_entered_still_runs_its_body():
    guarded = record._Guarded(BrokenWindow())
    ran = False

    with guarded.op_active("op-1"):
        ran = True

    assert ran, "dispatch must happen whether or not it could be tied to the record"


def test_a_child_that_was_not_asked_to_record_just_runs():
    with child_recording():
        pass


def test_a_child_closes_its_abandoned_commands_before_finishing_the_record(monkeypatch):
    order: list[str] = []

    class Session:
        def finish(self):
            order.append("finish")

    monkeypatch.setattr(record, "_resume", lambda: Session())
    monkeypatch.setattr(record, "close_open", lambda: order.append("close_open"))

    with child_recording():
        order.append("script")

    # Commands with no responses are part of what this process did, so they are closed while the
    # record is still open -- not after it has been flushed away.
    assert order == ["script", "close_open", "finish"]


def test_a_child_whose_record_cannot_be_finished_still_finishes(monkeypatch):
    class Session:
        def finish(self):
            raise RuntimeError("broken")

    monkeypatch.setattr(record, "_resume", lambda: Session())
    monkeypatch.setattr(record, "close_open", lambda: (_ for _ in ()).throw(RuntimeError("broken")))

    with child_recording():
        pass  # the script's outcome is what matters; the record's failure is not its problem
