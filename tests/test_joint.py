"""Running several jobs in one laboratory: `lc run --jobs` (schedule SPEC §6.11).

The plumbing is the upstream runner's -- `LabcodeRunner` subclasses it, and it has
taken a roster of jobs since 0.8. What labcode has to add is everything it does *per
workflow*, done per job instead, and one thing that is not merely repetition:

🔴 **identity.** Two jobs of one workflow bind the same port names and render the same
node paths, so a reproducible generator keyed on those alone gives one job's plate the
other's `_id` -- silently, and the same way on every run. The mint keys therefore carry
the job, and the trace records it, so an operation says whose work it was.

🔴 And only where there *is* a job: a run of a single workflow names none, and its keys
are exactly what they always were. The checked-in example observations are that
invariant; these tests pin the rule the keys follow.

The scheduler is a required dependency; these tests skip if it is not installed.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from labcode.idgen import SeededUuid4Generator
from labcode.objectid import inject_boundary_ids, inject_id_field
from labcode.record import ATTR_JOB

FIX = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parent.parent / "examples"
TWF = str(FIX / "transport.workflow.yaml")
TENV = str(FIX / "transport.env.yaml")


class WatchingRecorder:
    """Records the attributes every operation was opened with, and how the run ended."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, dict]] = []
        self.ended: tuple | None = None

    def run_started(self, *, mission_id=None):
        return "trace"

    def run_finished(self, *, error_type=None, message=None):
        self.ended = (error_type, message)

    def op_started(self, uuid, name, attributes):
        self.ops.append((name, dict(attributes)))

    def op_active(self, uuid):
        return contextlib.nullcontext()

    def op_finished(self, uuid, *, error_type=None, message=None, attributes=None):
        pass

    def child_env(self):
        return {}

    def shutdown(self):
        pass


def _ids(value, found: set | None = None) -> set:
    """Every ``_id`` anywhere in a value snapshot."""
    found = set() if found is None else found
    if isinstance(value, dict):
        if value.get("_id"):
            found.add(value["_id"])
        for item in value.values():
            _ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _ids(item, found)
    return found


def _run(workflow, **kwargs):
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    recorder = WatchingRecorder()
    runner = LabcodeRunner(
        workflow, TENV, seconds_per_tick=0.001, random_seed=0,
        running_task_margin=1, recorder=recorder, **kwargs,
    )
    try:
        runner.run()
    finally:
        runner.sim.close()
    return runner, recorder


def _jobs(*ids: str):
    from ofplang.run.runner import JobRequest, load_document

    doc = load_document(TWF)
    return [JobRequest(id=job_id, workflow=doc) for job_id in ids]


# -- identity ---------------------------------------------------------------------


def test_two_jobs_of_one_workflow_mint_different_identities():
    """🔴 The reason the job had to reach the mint. Both jobs run the same file, so
    every node path and port name they render is the same; without the job a seeded
    generator hands them one `_id` for two physical objects."""
    runner, _recorder = _run(_jobs("job1", "job2"))
    assert not runner.failed

    per_job = {job.id: _ids(job.values.snapshot()) for job in runner.jobs}
    assert all(per_job.values()), "each job should have minted something"
    assert not per_job["job1"] & per_job["job2"]


def test_the_job_is_in_the_mint_key_only_where_there_is_one():
    """🔴 The invariant that keeps every existing run reproducible *in the same way*: a
    run naming no job prefixes nothing, so its ids are the ones it always minted -- which
    is what makes the checked-in example observations a regression check rather than a
    snapshot that moves whenever the keys are touched.

    `plate_line` is used because it has a real Object entry input to mint for."""
    from ofplang.run.runner import load_document

    workflow = inject_id_field(load_document(EXAMPLES / "plate_line.workflow.yaml"))
    boundary = load_document(EXAMPLES / "plate_line.boundary.yaml")

    unnamed = inject_boundary_ids(boundary, workflow, SeededUuid4Generator())
    explicit_none = inject_boundary_ids(boundary, workflow, SeededUuid4Generator(), job=None)
    named = inject_boundary_ids(boundary, workflow, SeededUuid4Generator(), job="job1")
    other = inject_boundary_ids(boundary, workflow, SeededUuid4Generator(), job="job2")

    assert _ids(unnamed), "the example boundary should have an Object input to mint for"
    # Saying "no job" changes nothing, ...
    assert _ids(unnamed) == _ids(explicit_none)
    # ... naming one changes everything it mints, ...
    assert not _ids(unnamed) & _ids(named)
    # ... and two jobs of the same workflow and boundary get different ids.
    assert not _ids(named) & _ids(other)


# -- the record -------------------------------------------------------------------


def test_every_recorded_operation_says_which_job_it_was_for():
    _runner, recorder = _run(_jobs("job1", "job2"))
    jobs = {attributes.get(ATTR_JOB) for _name, attributes in recorder.ops}
    assert jobs == {"job1", "job2"}


def test_a_single_workflow_run_records_no_job_at_all():
    """A record of one workflow says exactly what it always said."""
    _runner, recorder = _run(TWF)
    assert recorder.ops
    assert all(ATTR_JOB not in attributes for _name, attributes in recorder.ops)


# -- what the record says when several jobs stop ----------------------------------


def test_the_record_gathers_every_job_that_stopped():
    """One run, one record -- so the reasons are gathered rather than the first one
    standing for all of them. `--on-job-failure continue` is what makes several
    unrelated reasons the ordinary case."""
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    runner = LabcodeRunner(
        _jobs("job1", "job2"), TENV, seconds_per_tick=0.001, random_seed=0,
        running_task_margin=1, recorder=WatchingRecorder(),
    )

    class Reason:
        def __init__(self, kind, detail):
            self.kind, self.detail = kind, detail

    runner.jobs[0].failure = Reason("activity_failed", "the arm dropped it")
    runner.jobs[1].failure = Reason("backend_refused_dispatch", "the rack was full")
    runner.failure = runner.jobs[0].failure

    error_type, message = runner._record_reason()
    runner.sim.close()
    assert error_type == "activity_failed"
    assert "job1: activity_failed: the arm dropped it" in message
    assert "job2: backend_refused_dispatch: the rack was full" in message


def test_a_single_workflow_reports_its_one_reason_unchanged():
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    runner = LabcodeRunner(
        TWF, TENV, seconds_per_tick=0.001, random_seed=0,
        running_task_margin=1, recorder=WatchingRecorder(),
    )

    class Reason:
        def __init__(self, kind, detail):
            self.kind, self.detail = kind, detail

    runner.failure = Reason("activity_failed", "the arm dropped it")
    error_type, message = runner._record_reason()
    runner.sim.close()
    assert (error_type, message) == ("activity_failed", "the arm dropped it")


# -- the entry itself --------------------------------------------------------------


def test_a_roster_may_not_also_carry_one_boundary_for_the_run():
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from ofplang.run.runner import RunnerError

    from labcode.runner import LabcodeRunner

    with pytest.raises(RunnerError, match="boundary per job"):
        LabcodeRunner(_jobs("job1"), TENV, {"boundary": {}}, recorder=WatchingRecorder())


# -- the CLI ------------------------------------------------------------------------


def _cli(*argv: str) -> tuple[int, str]:
    import io
    from contextlib import redirect_stderr

    from labcode.run_cli import main

    err = io.StringIO()
    with redirect_stderr(err):
        code = main(list(argv))
    return code, err.getvalue()


def test_lc_run_wants_one_form_or_the_other():
    code, err = _cli("--env", TENV)
    assert code == 2 and "either a WORKFLOW or --jobs" in err

    code, err = _cli(TWF, "--jobs", str(FIX / "joint.run.yaml"), "--env", TENV)
    assert code == 2 and "either a WORKFLOW or --jobs" in err


def test_a_roster_does_not_take_one_boundary_for_the_run():
    code, err = _cli(
        "--jobs", str(FIX / "joint.run.yaml"), "--env", TENV,
        "--boundary", str(FIX / "joint.run.yaml"),
    )
    assert code == 2 and "each job carries its own boundary" in err


def test_lc_run_jobs_drives_a_laboratory_of_two(tmp_path):
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    out = tmp_path / "status.yaml"
    code, _err = _cli(
        "--jobs", str(FIX / "joint.run.yaml"), "--env", TENV,
        "--seconds-per-tick", "0.001", "-o", str(out),
    )
    assert code == 0

    import yaml

    status = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert {entry["id"] for entry in status["jobs"]} == {"morning", "afternoon"}
    assert {a.get("job") for a in status["activities"]} == {"morning", "afternoon"}
