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

from ofplang.run.app import RunResult
from ofplang.run.runner import RollingRunner, RunnerError, load_document

from labcode.backend import DEFAULT_SECONDS_PER_TICK, labcode_backend_factory
from labcode.idgen import IdGenerator, SeededUuid4Generator
from labcode.objectid import inject_boundary_ids, inject_id_field, reserved_collisions


class LabcodeRunner(RollingRunner):
    """A `RollingRunner` that injects and mints labcode Object identities (`_id`).

    `workflow` is a path or an already-loaded document; `environment` is a path or dict.
    Object types are rewritten to declare ``_id``, the boundary's Object inputs are minted,
    and the labcode backend is wired with a shared `IdGenerator` (default: reproducible,
    seeded). `seconds_per_tick` / `speed` / `spawn` / `monotonic` / `sleep` configure the
    wall-clock backend; any other keyword is forwarded to `RollingRunner` (e.g.
    `random_seed`, `poll_interval`, `running_task_margin`, `observation_out`)."""

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
        self.id_generator: IdGenerator = id_generator or SeededUuid4Generator()
        rewritten = inject_id_field(doc)
        boundary = inject_boundary_ids(boundary, rewritten, self.id_generator)
        factory = labcode_backend_factory(
            seconds_per_tick=seconds_per_tick,
            speed=speed,
            id_generator=self.id_generator,
            spawn=spawn,
            monotonic=monotonic,
            sleep=sleep,
        )
        super().__init__(
            rewritten, environment, boundary, backend_factory=factory, **rolling_kwargs
        )


def run_labcode(
    workflow,
    environment,
    boundary: dict | None = None,
    *,
    running_task_margin: int = 0,
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
    `finally` whether the run finished or raised."""
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
    )
