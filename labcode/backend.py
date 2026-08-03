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

A script whose ``flavor`` is ``sila2`` is *wrapped* before it is handed to the child, so it
runs with a client open to each of its machines (`labcode.sila2`). The wrapping is pure
string synthesis here; the child stays a plain script runner that knows nothing about SiLA2.

The default cadence is coarse (``seconds_per_tick`` ~ tens of seconds): the effective
poll period is ``poll_interval * seconds_per_tick``, and a real device op wants to be
polled at a human-observable cadence, not sub-second (which would flood the replan loop).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable

from ofplang.run.simulator import (
    DeviceComputationError,
    SubprocessBackend,
    default_device_model,
)

from labcode.extension import (
    FLAVOR_SILA2,
    device_connections,
    python_code,
    script_flavor,
    transporter_connections,
)
from labcode.idgen import DEFAULT_ID_GENERATOR, IdGenerator
from labcode.objectid import stamp_object_ids
from labcode.sila2 import failing_code, targets_of, wrap

# Real seconds per environment time tick (default). With poll_interval=1 this makes the
# effective poll period ~20 s -- coarse enough that a real op's dispatch/running/complete
# reads as discrete, observable steps rather than a burst of sub-second replans.
DEFAULT_SECONDS_PER_TICK = 20.0


def _flavored(code: str, script, identifiers, connections, label: str) -> str:
    """`code` as its flavor asks for it: a `sila2` script wrapped so its clients are open
    (SPECIFICATIONS.md §1.1), a `raw` one unchanged.

    `identifiers` are the machines the operation may connect to -- a mode's devices, or a
    route's transporter. With none of them reachable the operation cannot run at all, which
    is returned as *failing code* rather than raised: a resolver runs inside dispatch, where
    an exception would escape the run instead of failing one operation. The dialect front
    door rejects this case up front, so this is a backstop."""
    if script_flavor(script) != FLAVOR_SILA2:
        return code
    targets = targets_of(identifiers, connections)
    if not targets:
        return failing_code(
            f"labcode: {label} declares x-labcode.script.flavor 'sila2' but none of its "
            f"machines declares an x-labcode.connection"
        )
    return wrap(code, targets)


def make_code_resolver(environment: dict) -> Callable:
    """Build the labcode code resolver, closed over `environment` (the raw, mode-id-
    normalised env dict, which keeps the ``x-labcode`` extension keys).

    The returned ``resolver(process, mode, inputs, definition) -> str | None`` maps the
    dispatched ``(process, mode id)`` to its env ``x-labcode.script.code`` (2) -- wrapped
    with the SiLA2 clients of the mode's devices when the script's flavor asks for that;
    failing that, to the workflow definition's own ``script.code`` (1); failing that,
    ``None`` (a typed-default no-op). Only ``language: python`` scripts are run (the dialect
    validator rejects anything else at the front door, so a non-python script here is
    treated as absent rather than mis-run)."""
    modes_by_process: dict[str, dict] = {}
    for name, proc in (environment.get("processes") or {}).items():
        if not isinstance(proc, dict):
            continue
        modes_by_process[name] = {
            m.get("id"): m for m in (proc.get("modes") or []) if isinstance(m, dict)
        }
    connections = device_connections(environment)

    def resolver(process, mode, inputs, definition) -> str | None:
        # (2) env x-labcode.script on the dispatched mode.
        mode_entry = (modes_by_process.get(process) or {}).get(str(mode)) or {}
        xlab = mode_entry.get("x-labcode")
        if isinstance(xlab, dict):
            script = xlab.get("script")
            code = python_code(script)
            if code is not None:
                return _flavored(
                    code, script, mode_entry.get("devices"), connections,
                    f"process {process!r} mode {mode!r}",
                )
        # (1) the workflow's own script process (v0 §22). A workflow script carries no
        # flavor: the dialect lives in the environment.
        return python_code((definition or {}).get("script"))

    return resolver


def make_transport_resolver(environment: dict) -> Callable:
    """Build the labcode transport code resolver, closed over `environment`.

    The returned ``resolver(transporter, from_spot, to_spot) -> str | None`` maps a
    dispatched transport to its env ``transports[]`` route's ``x-labcode.script.code``,
    matched exactly on ``(transporter, from, to)`` -- wrapped with the transporter's SiLA2
    client when the script's flavor asks for that; None when the route has no script (the
    transport then runs as a plain timed move -- bookkeeping only, no device command)."""
    connections = transporter_connections(environment)
    routes: dict[tuple, str | None] = {}
    for transport in environment.get("transports") or []:
        if not isinstance(transport, dict):
            continue
        transporter = transport.get("transporter")
        key = (transporter, transport.get("from"), transport.get("to"))
        xlab = transport.get("x-labcode")
        script = xlab.get("script") if isinstance(xlab, dict) else None
        code = python_code(script)
        if code is not None:
            code = _flavored(
                code, script, [transporter], connections,
                f"transport {transporter!r} "
                f"{transport.get('from')} -> {transport.get('to')}",
            )
        routes[key] = code

    def resolver(transporter, from_spot, to_spot) -> str | None:
        return routes.get((transporter, from_spot, to_spot))

    return resolver


def _labcode_child_spawn(job: dict):
    """Launch the labcode child (`python -m labcode._child`), feeding the job JSON on
    stdin. Same shape as ofplang-run's default spawn (result via ``job["result_path"]``,
    stderr captured), but runs the labcode partial-output child."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "labcode._child"],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(job))
    proc.stdin.close()
    return proc


class LabcodeBackend(SubprocessBackend):
    """A `SubprocessBackend` for the labcode dialect: partial process outputs + transport
    execution.

    Two dialect behaviours on top of the base backend:

    * **Transport execution.** A dispatched transport whose env route carries an
      ``x-labcode.script`` runs it in a child process, with the physical route and the
      moved Object's view bound as locals (``from_spot`` / ``to_spot`` / ``transporter`` /
      ``view``). It reuses the upstream machinery -- `_start_child_op` launches it and the
      settle loop completes / fails it (ofplang-run >= 0.1.6) -- so only dispatch is added.
    * **Partial process outputs** (SPECIFICATIONS.md §1.2). A process script returns only
      the outputs it computes; `_resolve_model` fills the rest from `default_device_model`
      (carrying an Object output via ``objects.map``, defaulting the others) and merges the
      script's values on top. A returned name that is not a declared output is an error.
      This needs the partial-tolerant `labcode._child` (the default `spawn`), since the
      upstream child verifies outputs exactly."""

    def __init__(
        self,
        environment,
        *,
        transport_resolver: Callable | None = None,
        id_generator: IdGenerator | None = None,
        spawn=None,
        **kwargs,
    ):
        super().__init__(environment, spawn=spawn or _labcode_child_spawn, **kwargs)
        self._transport_resolver = transport_resolver or (lambda *args: None)
        # Mints the reserved `_id` on created Objects (see `labcode.objectid`). Shared
        # with the run boundary's minting so a run's ids are consistent; default is the
        # reproducible seeded generator.
        self._id_gen = id_generator or DEFAULT_ID_GENERATOR

    def _resolve_model(self, process, mode, inputs, output_schema, definition, node=None):
        """The value model `_complete` calls at completion: merge the script's *partial*
        outputs onto the defaults (§1.2), then stamp Object identities (`_id`).

        `default_device_model` supplies the base (``objects.map`` Object carry + typed
        defaults); the script's returned values override it. A returned name outside
        `output_schema` is a runtime failure. `stamp_object_ids` then mints ``_id`` for a
        created Object and carries it for a mapped one (keyed by `node`, the workflow
        provenance). A timed op (no child) or a child error is handled as in the base
        backend."""
        pending = self._pending
        if not isinstance(pending, dict):  # the _TIMED sentinel: no child ran
            outputs = default_device_model(process, mode, inputs, output_schema, definition)
            return stamp_object_ids(
                outputs, definition, inputs or {}, node, self._id_gen, output_schema
            )
        if "error" in pending:
            err = pending["error"]
            raise DeviceComputationError(
                err.get("message", "child failed"), code=err.get("code", "child_error")
            )
        raw = pending.get("outputs") or {}
        extra = set(raw) - set(output_schema or {})
        if extra:
            raise DeviceComputationError(
                f"script process {process!r} returned undeclared output names {sorted(extra)}",
                code="script_output_names",
            )
        base = default_device_model(process, mode, inputs, output_schema, definition)
        return stamp_object_ids(
            {**base, **raw}, definition, inputs or {}, node, self._id_gen, output_schema
        )

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
    id_generator: IdGenerator | None = None,
    spawn: Callable[[dict], object] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[dict], SubprocessBackend]:
    """Build a ``backend_factory(environment) -> SubprocessBackend`` for the runner,
    wired with the labcode env-script resolver (`make_code_resolver`).

    `seconds_per_tick` / `speed` set the wall-clock pace (see module docstring).
    `id_generator` mints the reserved Object ``_id`` (default: the reproducible seeded
    generator; pass a `RealUuid4Generator` for a real run, or share the same instance the
    run boundary mints with). `spawn` overrides how a child is launched (default: a real
    subprocess); `monotonic` / `sleep` are injectable so a test can drive the pacing on a
    fake clock."""

    def factory(environment: dict) -> LabcodeBackend:
        kwargs: dict = {
            "resolver": make_code_resolver(environment),
            "transport_resolver": make_transport_resolver(environment),
            "id_generator": id_generator,
            "seconds_per_tick": seconds_per_tick,
            "speed": speed,
            "monotonic": monotonic,
            "sleep": sleep,
        }
        if spawn is not None:
            kwargs["spawn"] = spawn
        return LabcodeBackend(environment, **kwargs)

    return factory
