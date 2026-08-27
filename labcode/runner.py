"""`LabcodeRunner` -- the rolling-horizon runner with labcode's Object identity built in.

A labcode run always carries the reserved Object ``_id`` (see `labcode.objectid`). Rather
than scatter that setup across every entry point, `LabcodeRunner` owns it: at construction
it rewrites the workflow's Object types to declare ``_id``, mints the run boundary's Object
ids, and wires the labcode subprocess backend with the *same* `IdGenerator` (so created and
boundary Objects draw consistent, reproducible ids). It then drives the workflow exactly
like `ofplang.run.RollingRunner`, which it subclasses -- the rewrite is possible with no
temp file because the runner accepts an in-memory workflow document.

`LabcodeRunner` (or `run_labcode`) is therefore the single, canonical way to run labcode:
the ``_id`` invariant holds by construction, so `stamp_object_ids` treats a missing ``_id``
as an error rather than tolerating it. A bare `labcode_backend_factory` remains an internal
building block; using it on an Object workflow without this runner fails (no rewrite).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from ofplang.run.app import RunResult
from ofplang.run.runner import RollingRunner, RunnerError, load_document

from labcode.backend import (
    DEFAULT_SECONDS_PER_TICK,
    FROM_ENVIRONMENT,
    labcode_backend_factory,
)
from labcode.idgen import IdGenerator, RealUuid4Generator, SeededUuid4Generator
from labcode.objectid import inject_boundary_ids, inject_id_field, reserved_collisions
from labcode.probe import CadenceReporter, ChangeReporter, Prober
from labcode.record import Recorder, build_recorder


class LabcodeRunner(RollingRunner):
    """A `RollingRunner` that injects and mints labcode Object identities (`_id`).

    `workflow` and `environment` are each a path or an already-loaded document (the
    `lc run` front door passes the environment it has already read for its dialect check,
    so the file is not read twice). Object types are rewritten to declare ``_id``, the boundary's
    Object inputs are minted, and the labcode backend is wired with a shared `IdGenerator`
    (default: reproducible, seeded). `seconds_per_tick` / `speed` / `spawn` / `monotonic` /
    `sleep` configure the wall-clock backend, `probe` / `prober` /
    `on_availability_change` its availability probing (`labcode.probe`), and `op_timeout`
    how long one operation may run before it is stopped and failed (default: whatever the
    environment's ``x-labcode.op_timeout`` says); any other keyword is forwarded to
    `RollingRunner` (e.g. `random_seed`, `down_scope`, `observation_out`, `max_ticks`).

    `trace` records what the run did (`labcode.record`; off by default, so a run that says
    nothing pays nothing). `mission_id` is recorded with it and given no meaning here --
    several runs may share one, and labcode neither reads it nor checks it. `on_trace_id` is
    called once with the id an operator can find the record by, as the run starts, so a CLI
    can print it while the run is still going. `recorder` overrides where the record goes,
    which is how a test watches what a run records with no recording backend installed.

    **`trace` also changes the default `_id` generator** to a real (random) one, because a
    reproducible generator gives the same identity to the same port on every run -- fine for
    stable example output, wrong for asking what happened to one physical plate. An explicit
    `id_generator` still wins, so the two are independent.

    **`running_task_margin` defaults to the poll interval here**, not to the upstream 0.
    The margin is how far ahead of *now* a still-running operation is assumed to finish
    (`max(reported end, now + margin)` when the scheduler pins it), so a positive margin is
    what keeps a successor from being planned at `now` and dispatched onto a resource the
    predecessor has not released. With 0 a real operation -- which overruns its estimate as
    a matter of course -- can have its successor dispatched onto a still-busy device, which
    fails the whole run rather than one operation. Upstream can default to 0 because its
    home ground is a deterministic simulation with no overruns; labcode's is hardware."""

    def __init__(
        self,
        workflow,
        environment,
        boundary: dict | None = None,
        *,
        seconds_per_tick: float = DEFAULT_SECONDS_PER_TICK,
        speed: float = 1.0,
        id_generator: IdGenerator | None = None,
        spawn: Callable[[dict], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        probe: bool = True,
        prober: Prober | None = None,
        on_availability_change: ChangeReporter | None = None,
        on_cadence_slip: CadenceReporter | None = None,
        op_timeout: float | None | Literal["environment"] = FROM_ENVIRONMENT,
        poll_interval: int | None = 1,
        running_task_margin: int | None = None,
        trace: bool = False,
        mission_id: str | None = None,
        on_trace_id: Callable[[str], None] | None = None,
        recorder: Recorder | None = None,
        **rolling_kwargs,
    ) -> None:
        doc = workflow if isinstance(workflow, dict) else load_document(workflow)
        if not isinstance(doc, dict):
            raise RunnerError("workflow must be a mapping")
        clashes = reserved_collisions(doc)
        if clashes:
            raise RunnerError(
                f"type(s) {clashes} declare the reserved view field '_id'; labcode owns it "
                f"as an implicit Object identity and it must not be declared"
            )
        # A recorded run mints **real** ids by default: a reproducible generator gives the same
        # `_id` to the same port on every run, which is what makes the checked-in example
        # observations stable -- and what would make "everything that happened to this plate"
        # collapse several runs' plates into one. The two are independent knobs, though: an
        # explicit `id_generator` always wins, so a recorded run can still be reproducible.
        self.id_generator: IdGenerator = id_generator or (
            RealUuid4Generator() if trace else SeededUuid4Generator()
        )
        # `trace` says this run is recorded; `recorder` says where. Passing a recorder is how a
        # test watches what a run records without a recording backend installed.
        self.recorder: Recorder = (
            recorder if recorder is not None else build_recorder(enabled=trace)
        )
        self.mission_id = mission_id
        self._on_trace_id = on_trace_id
        #: The id an operator can find this run's record by, once `run` has started it.
        self.trace_id: str | None = None
        rewritten = inject_id_field(doc)
        boundary = inject_boundary_ids(boundary, rewritten, self.id_generator)
        factory = labcode_backend_factory(
            seconds_per_tick=seconds_per_tick,
            speed=speed,
            id_generator=self.id_generator,
            spawn=spawn,
            monotonic=monotonic,
            sleep=sleep,
            probe=probe,
            prober=prober,
            on_availability_change=on_availability_change,
            on_cadence_slip=on_cadence_slip,
            op_timeout=op_timeout,
            recorder=self.recorder,
        )
        # A margin of at least one tick, defaulting to the poll interval (see the class
        # docstring). An explicit value is honoured as given -- including 0, for a caller
        # that knows its operations cannot overrun. Event-boundary advance
        # (`poll_interval=None`) has no interval to follow, so one tick is the default there.
        if running_task_margin is None:
            running_task_margin = poll_interval if poll_interval is not None else 1
        super().__init__(
            rewritten,
            environment,
            boundary,
            backend_factory=factory,
            poll_interval=poll_interval,
            running_task_margin=running_task_margin,
            **rolling_kwargs,
        )

    def run(self) -> dict:
        """Drive the workflow to completion, recording the run around it.

        The record is opened here and closed here, whatever happens in between: a run that
        stopped on a failure records why (the runner's own reason), and one that raised records
        the exception -- an unclosed record would look like a run that never ended."""
        self.trace_id = self.recorder.run_started(mission_id=self.mission_id)
        if self.trace_id is not None and self._on_trace_id is not None:
            self._on_trace_id(self.trace_id)
        try:
            status = super().run()
        except BaseException as exc:
            self._finish_record(type(exc).__name__, str(exc))
            raise
        failure = self.failure
        self._finish_record(*((failure.kind, failure.detail) if failure else (None, None)))
        return status

    def _finish_record(self, error_type: str | None, message: str | None) -> None:
        self.recorder.run_finished(error_type=error_type, message=message)
        self.recorder.shutdown()


def run_labcode(
    workflow,
    environment,
    boundary: dict | None = None,
    *,
    running_task_margin: int | None = None,
    random_seed: int | None = None,
    poll_interval: int | None = 1,
    seconds_per_tick: float = DEFAULT_SECONDS_PER_TICK,
    speed: float = 1.0,
    id_generator: IdGenerator | None = None,
    observation_out: str | None = None,
    **kwargs,
) -> RunResult:
    """Drive `workflow` on the labcode backend (with Object ``_id`` injected/minted) to
    completion and return a `RunResult` -- the labcode analogue of `ofplang.run.run_workflow`.

    The caller is expected to have validated the workflow (the `lc run` front doors); this
    runs trusting. The labcode backend holds child processes, so its `close` is called in a
    `finally` whether the run finished or raised.

    `running_task_margin` defaults to the poll interval, as it does on `LabcodeRunner` (and
    for the reason given there): a real operation overruns, and a margin of 0 lets its
    successor be dispatched onto a resource it has not released."""
    runner = LabcodeRunner(
        workflow,
        environment,
        boundary,
        seconds_per_tick=seconds_per_tick,
        speed=speed,
        id_generator=id_generator,
        running_task_margin=running_task_margin,
        random_seed=random_seed,
        poll_interval=poll_interval,
        observation_out=observation_out,
        **kwargs,
    )
    try:
        status = runner.run()
    finally:
        close = getattr(runner.sim, "close", None)
        if callable(close):
            close()
    return RunResult(
        status=status,
        result_boundary=runner.result_boundary,
        failed=runner.failed,
        failure=runner.failure,
        # Warnings the scheduler raised, one per distinct code. `RunResult` defaults
        # this to empty, so leaving it out would silently drop them on this route only
        # (ofplang-run >= 0.2.0 collects them).
        scheduler_warnings=runner.scheduler_warnings,
    )
