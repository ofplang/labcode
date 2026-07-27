"""labcode execution backend: run env-supplied device scripts out-of-process.

labcode drives a workflow on the ofplang `SubprocessBackend` (real, wall-clock-paced,
out-of-process script execution), but sources each device operation's script from the
*environment* -- an ``x-labcode.script`` extension on a process mode -- rather than the
workflow. That is the labcode dialect's device model: the environment says how each
``(process, mode)`` is physically carried out.

`labcode_backend_factory` builds the ``backend_factory(environment) -> Backend`` the
runner calls. It closes a *code resolver* over the (raw, mode-id-normalised) environment
dict -- which still carries the ``x-labcode`` keys, unlike the parsed ``Environment`` --
so the resolver can look a mode's script up by ``(process, mode id)``. Resolution order
(labcode dialect, mutually exclusive per process):

1. the env ``x-labcode.script`` on the dispatched mode (the labcode device script), else
2. the workflow's own ``script`` (a v0 §22 script process), else
3. ``None`` -- the op runs as a timed, typed-default no-op (a device not yet scripted);
   `labcode.dialect` warns about those at the front door.

The default cadence is coarse (``seconds_per_tick`` ~ tens of seconds): the effective
poll period is ``poll_interval * seconds_per_tick``, and a real device op wants to be
polled at a human-observable cadence, not sub-second (which would flood the replan loop).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ofplang.run.simulator import SubprocessBackend

# Real seconds per environment time tick (default). With poll_interval=1 this makes the
# effective poll period ~20 s -- coarse enough that a real op's dispatch/running/complete
# reads as discrete, observable steps rather than a burst of sub-second replans.
DEFAULT_SECONDS_PER_TICK = 20.0


def make_code_resolver(environment: dict) -> Callable:
    """Build the labcode code resolver, closed over `environment` (the raw, mode-id-
    normalised env dict, which keeps the ``x-labcode`` extension keys).

    The returned ``resolver(process, mode, inputs, definition) -> str | None`` maps the
    dispatched ``(process, mode id)`` to its env ``x-labcode.script.code`` (2); failing
    that, to the workflow definition's own ``script.code`` (1); failing that, ``None``
    (a typed-default no-op). Only ``language: python`` scripts are run (the dialect
    validator rejects anything else at the front door, so a non-python script here is
    treated as absent rather than mis-run)."""
    modes_by_process: dict[str, dict] = {}
    for name, proc in (environment.get("processes") or {}).items():
        if not isinstance(proc, dict):
            continue
        modes_by_process[name] = {
            m.get("id"): m for m in (proc.get("modes") or []) if isinstance(m, dict)
        }

    def _python_code(script) -> str | None:
        if isinstance(script, dict) and script.get("language") == "python":
            return script.get("code") or ""
        return None

    def resolver(process, mode, inputs, definition) -> str | None:
        # (2) env x-labcode.script on the dispatched mode.
        mode_entry = (modes_by_process.get(process) or {}).get(str(mode)) or {}
        xlab = mode_entry.get("x-labcode")
        if isinstance(xlab, dict):
            code = _python_code(xlab.get("script"))
            if code is not None:
                return code
        # (1) the workflow's own script process (v0 §22).
        return _python_code((definition or {}).get("script"))

    return resolver


def labcode_backend_factory(
    *,
    seconds_per_tick: float = DEFAULT_SECONDS_PER_TICK,
    speed: float = 1.0,
    spawn: Callable[[dict], object] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[dict], SubprocessBackend]:
    """Build a ``backend_factory(environment) -> SubprocessBackend`` for the runner,
    wired with the labcode env-script resolver (`make_code_resolver`).

    `seconds_per_tick` / `speed` set the wall-clock pace (see module docstring). `spawn`
    overrides how a child is launched (default: a real subprocess); `monotonic` / `sleep`
    are injectable so a test can drive the pacing on a fake clock."""

    def factory(environment: dict) -> SubprocessBackend:
        kwargs: dict = {
            "resolver": make_code_resolver(environment),
            "seconds_per_tick": seconds_per_tick,
            "speed": speed,
            "monotonic": monotonic,
            "sleep": sleep,
        }
        if spawn is not None:
            kwargs["spawn"] = spawn
        return SubprocessBackend(environment, **kwargs)

    return factory
