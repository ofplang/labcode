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


def _python_code(script) -> str | None:
    """The `code` of a python `x-labcode.script` mapping, or None (absent / non-python).
    A non-python script is treated as absent here; the dialect validator rejects it at the
    front door, so it is never silently mis-run."""
    if isinstance(script, dict) and script.get("language") == "python":
        return script.get("code") or ""
    return None


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


def make_transport_resolver(environment: dict) -> Callable:
    """Build the labcode transport code resolver, closed over `environment`.

    The returned ``resolver(transporter, from_spot, to_spot) -> str | None`` maps a
    dispatched transport to its env ``transports[]`` route's ``x-labcode.script.code``,
    matched exactly on ``(transporter, from, to)``; None when the route has no script (the
    transport then runs as a plain timed move -- bookkeeping only, no device command)."""
    routes: dict[tuple, str | None] = {}
    for transport in environment.get("transports") or []:
        if not isinstance(transport, dict):
            continue
        key = (transport.get("transporter"), transport.get("from"), transport.get("to"))
        xlab = transport.get("x-labcode")
        routes[key] = _python_code(xlab.get("script")) if isinstance(xlab, dict) else None

    def resolver(transporter, from_spot, to_spot) -> str | None:
        return routes.get((transporter, from_spot, to_spot))

    return resolver


class LabcodeBackend(SubprocessBackend):
    """A `SubprocessBackend` that also runs *transport* scripts out-of-process.

    The base backend runs processing scripts (via its resolver) and leaves transports
    timed. labcode adds transport execution: a dispatched transport whose env route
    carries an ``x-labcode.script`` runs that script in a child process, with the physical
    route and the moved Object's view bound as locals (``from_spot`` / ``to_spot`` /
    ``transporter`` / ``view``). It reuses the upstream machinery entirely -- the shared
    `_start_child_op` launches it, and the settle loop completes it (moving material) or
    fails it on a child error (ofplang-run >= 0.1.6) -- so only dispatch is overridden."""

    def __init__(self, environment, *, transport_resolver: Callable | None = None, **kwargs):
        super().__init__(environment, **kwargs)
        self._transport_resolver = transport_resolver or (lambda *args: None)

    def dispatch_transport(self, transporter, from_spot, to_spot, duration=None, view=None) -> str:
        uuid = super().dispatch_transport(
            transporter, from_spot, to_spot, duration=duration, view=view
        )
        code = self._transport_resolver(transporter, from_spot, to_spot)
        if code is not None:
            self._start_child_op(
                uuid, code=code, kind="transport",
                inputs={"from_spot": from_spot, "to_spot": to_spot,
                        "transporter": transporter, "view": view},
            )
        return uuid


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

    def factory(environment: dict) -> LabcodeBackend:
        kwargs: dict = {
            "resolver": make_code_resolver(environment),
            "transport_resolver": make_transport_resolver(environment),
            "seconds_per_tick": seconds_per_tick,
            "speed": speed,
            "monotonic": monotonic,
            "sleep": sleep,
        }
        if spawn is not None:
            kwargs["spawn"] = spawn
        return LabcodeBackend(environment, **kwargs)

    return factory
