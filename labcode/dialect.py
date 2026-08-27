"""labcode dialect front-door validation of the ``x-labcode`` extension (P5).

`ofplang-validate` checks a workflow is portable v0; `ofplang-schedule` merely tolerates
``x-`` extension keys in an environment (it neither interprets nor checks them -- at *any*
position, root and process included). labcode *owns* the ``x-labcode`` extension, so this
module is its conformance validator -- the reference implementation of the labcode dialect
spec (`SPECIFICATIONS.md`). `lc run` calls it at the front door, after the shared workflow
front door, over the workflow (needed for the (1)/(2) exclusivity rule) and the environment
(where ``x-labcode`` lives).

Three kinds of check:

* **position** -- an ``x-labcode`` outside the places this version reads (the environment
  root, a process mode, a transport route, a device, a transporter) is an error. Nothing
  else would ever read it, and `ofplang-schedule` will not complain either, so a misplaced
  block would otherwise be silent for good. (An ``x-labcode`` in the *workflow* is
  deliberately not checked: that document is portable v0, read by other implementations,
  and policing other people's extension keys there is not labcode's business.)
* **shape** -- the keys are closed: an unknown key is an error rather than something
  ignored, so a misspelled ``flavour:`` or a ``probe:`` this version cannot honour is
  reported instead of quietly doing nothing.
* **relational / runnability** --
  - **(1)/(2) exclusive**: a process must not carry both a workflow ``script`` (1) and an
    env ``x-labcode.script`` (2); that is ambiguous.
  - **a `sila2` script needs somewhere to connect**: its mode must have a device (or its
    route a transporter) that declares an ``x-labcode.connection``.
  - **a `sila2` script's client names are reserved**: the process must not declare an input
    port that one of them would overwrite.
  - **a probe needs an address**: a machine whose effective ``x-labcode.probe`` is enabled
    must declare an ``x-labcode.connection``. A policy that nothing enables is a *warning*
    (it does nothing, which is unlikely to be what its author meant).
  - **typed-default reachability** (a *warning*, not an error): a process with neither a
    workflow script nor any mode's ``x-labcode.script`` will run as a typed-default no-op
    (a device not yet scripted). Allowed -- convenient while mocking -- but surfaced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from labcode.extension import (
    EXTENSION_KEY,
    FLAVOR_SILA2,
    FLAVORS,
    NODE_KEYS,
    RESERVED_LOCALS,
    ROOT_KEYS,
    SCRIPT_KEYS,
    SCRIPT_SITE_KEYS,
    TLS_INSECURE_NOT_DECLARED,
    TLS_UNSUPPORTED,
    TRANSPORT_SCRIPT_KEYS,
    find_extensions,
    format_path,
    is_supported_position,
    merge_probe,
    parse_connection,
    parse_op_timeout,
    parse_probe,
    script_flavor,
    spot_device,
    unknown_key_messages,
)
from labcode.objectid import RESERVED_ID, reserved_collisions


@dataclass
class DialectResult:
    """Outcome of `validate_dialect`: ``ok`` iff there are no errors. ``errors`` block the
    run (a usage error); ``warnings`` are surfaced but do not block."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _NodeIndex:
    """What the environment says about its ``devices[]`` (or ``transporters[]``): which
    ids exist, and which of them say how to connect. A *declared* connection counts here
    even if it is malformed -- that is reported once, where it is, rather than a second
    time as "nothing to connect to"."""

    declared: frozenset[str]
    with_connection: frozenset[str]


def validate_dialect(workflow: dict, environment: dict) -> DialectResult:
    """Validate the labcode ``x-labcode`` extension (P5) over `workflow` and
    `environment`. Returns a `DialectResult` (errors + warnings); the caller reports it
    and maps a non-ok result to a usage error."""
    errors: list[str] = []
    warnings: list[str] = []

    # Reserved Object identity: labcode injects `_id` into every Object type's view (an
    # implicit, value-layer identity). A user type that already declares `_id` collides
    # with that -- reject it rather than clobber the user's field.
    for type_name in reserved_collisions(workflow):
        errors.append(
            f"type {type_name!r} declares the reserved view field {RESERVED_ID!r}; "
            f"labcode owns it as an implicit Object identity and it must not be declared"
        )

    _validate_positions(environment, errors)
    root_probe = _validate_root(environment, errors)
    devices = _node_index(environment, "devices")
    transporters = _node_index(environment, "transporters")
    probed = _validate_nodes(
        environment, "devices", "device", root_probe, errors, warnings
    )
    probed |= _validate_nodes(
        environment, "transporters", "transporter", root_probe, errors, warnings
    )
    probed |= _validate_nodes(
        environment, "replenishers", "replenisher", root_probe, errors, warnings
    )
    if root_probe and not probed and root_probe.get("enabled") is not False:
        # A document-wide policy that reaches nothing: the run will probe no machine at
        # all, which is not what writing one says. An explicit `enabled: false` says it.
        warnings.append(
            "environment root: x-labcode.probe is declared but no machine is probed; it "
            "has no effect (add `enabled: true`, and a connection to the machines to check)"
        )
    _validate_processes(workflow, environment, devices, errors, warnings)
    _validate_transports(environment, transporters, devices, errors, warnings)
    _validate_replenishments(environment, errors, warnings)

    return DialectResult(ok=not errors, errors=errors, warnings=warnings)


# -- position ------------------------------------------------------------------


_BELONGS = (
    "it belongs at the environment root (probing defaults), on a process mode "
    "(processes.<p>.modes[]), a transport route (transports[]), a refill route "
    "(replenishments[]), a device (devices[]), a transporter (transporters[]) or a "
    "replenisher (replenishers[])"
)


def _validate_positions(environment: dict, errors: list) -> None:
    """Reject an ``x-labcode`` where nothing reads one."""
    for path, _value in find_extensions(environment):
        if not is_supported_position(path):
            errors.append(f"x-labcode at {format_path(path)} is not supported; {_BELONGS}")


def _validate_root(environment: dict, errors: list) -> dict:
    """Shape-check the environment root's ``x-labcode`` -- document-wide defaults, and
    nothing else (an address belongs to the machine that has it). Returns the probe
    fields it declares, for the effective policies below."""
    extension = environment.get(EXTENSION_KEY)
    if extension is None:
        return {}
    if not isinstance(extension, dict):
        errors.append("environment root: x-labcode must be a mapping")
        return {}
    errors.extend(
        f"environment root: {message}"
        for message in unknown_key_messages(extension, "x-labcode", ROOT_KEYS)
    )
    # The operation timeout is one value for the whole lab, so it lives here and nowhere
    # else -- a mode that needs a different wait says so inside its own script.
    if "op_timeout" in extension:
        _value, messages = parse_op_timeout(extension["op_timeout"])
        errors.extend(f"environment root: x-labcode.{message}" for message in messages)
    raw = extension.get("probe")
    if raw is None:
        return {}
    declared, messages = parse_probe(raw)
    errors.extend(f"environment root: x-labcode.{message}" for message in messages)
    return declared


# -- devices and transporters ---------------------------------------------------


def _node_index(environment: dict, key: str) -> _NodeIndex:
    declared: set[str] = set()
    with_connection: set[str] = set()
    entries = environment.get(key)
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier = entry.get("id")
            if not isinstance(identifier, str):
                continue
            declared.add(identifier)
            extension = entry.get(EXTENSION_KEY)
            if isinstance(extension, dict) and extension.get("connection") is not None:
                with_connection.add(identifier)
    return _NodeIndex(frozenset(declared), frozenset(with_connection))


def _validate_nodes(
    environment: dict, key: str, kind: str, root_probe: dict, errors: list, warnings: list
) -> bool:
    """Shape-check the ``x-labcode`` on each ``devices[]`` / ``transporters[]`` entry: how
    to reach the machine (`connection`) and whether to check that it answers (`probe`).

    Returns whether any machine here ends up probed, which the caller uses to tell a
    policy that does nothing from one that does."""
    entries = environment.get(key)
    if not isinstance(entries, list):
        return False
    connected: set[str] = set()
    any_probed = False
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        extension = entry.get(EXTENSION_KEY)
        identifier = entry.get("id")
        label = f"{kind} {identifier!r}" if isinstance(identifier, str) else f"{kind} #{index}"
        if extension is not None and not isinstance(extension, dict):
            errors.append(f"{label}: x-labcode must be a mapping")
            continue
        extension = extension or {}
        errors.extend(
            f"{label}: {message}"
            for message in unknown_key_messages(extension, "x-labcode", NODE_KEYS)
        )

        # The address, if it has one. Note that the probe rule below only asks whether a
        # `connection` was declared: its own faults are reported here, once.
        raw = extension.get("connection")
        if raw is not None:
            if isinstance(identifier, str):
                if identifier in connected:
                    errors.append(
                        f"{label}: {key}[] declares this id more than once with an "
                        f"x-labcode.connection, so which one to reach is ambiguous"
                    )
                connected.add(identifier)
            connection, messages = parse_connection(raw)
            errors.extend(f"{label}: x-labcode.{message}" for message in messages)
            if connection is not None and not connection.insecure:
                reason = TLS_UNSUPPORTED if "insecure" in raw else TLS_INSECURE_NOT_DECLARED
                errors.append(f"{label}: x-labcode.{reason}")

        # The probing policy: the root's defaults under this machine's own.
        own = extension.get("probe")
        declared: dict = {}
        if own is not None:
            declared, messages = parse_probe(own)
            errors.extend(f"{label}: x-labcode.{message}" for message in messages)
        policy = merge_probe(root_probe, declared)
        if policy.enabled:
            if raw is None:
                errors.append(
                    f"{label}: x-labcode.probe is enabled but the machine declares no "
                    f"x-labcode.connection, so there is no address to probe"
                )
            else:
                any_probed = True
        elif declared and declared.get("enabled") is not False:
            # A policy written on the machine that nothing turns on: allowed (the default
            # is off, deliberately) but surfaced, since it reads as a request to monitor.
            # An explicit `enabled: false` is the opposite -- excluding this machine from a
            # document-wide sweep is exactly how that is meant to be written.
            warnings.append(
                f"{label}: x-labcode.probe is declared but not enabled; it has no effect "
                f"(add `enabled: true` here or at the environment root)"
            )
    return any_probed


# -- scripts --------------------------------------------------------------------


def _validate_script(
    script: Any, label: str, errors: list, allowed: Sequence[str] = SCRIPT_KEYS
) -> bool:
    """Shape-check an ``x-labcode.script``. Returns False if it is not a mapping at all
    (nothing further can be said about it).

    `allowed` is the closed key set for *where* this script sits: a transport route may say
    one thing a process mode may not (`TRANSPORT_SCRIPT_KEYS`)."""
    if not isinstance(script, dict):
        errors.append(f"{label}: x-labcode.script must be a mapping")
        return False
    errors.extend(
        f"{label}: {message}"
        for message in unknown_key_messages(script, "x-labcode.script", allowed)
    )
    language = script.get("language")
    if language != "python":
        errors.append(f"{label}: x-labcode.script.language must be 'python' (got {language!r})")
    if not isinstance(script.get("code"), str):
        errors.append(f"{label}: x-labcode.script.code must be a string")
    flavor = script.get("flavor")
    if flavor is not None and flavor not in FLAVORS:
        errors.append(
            f"{label}: x-labcode.script.flavor must be one of "
            f"{', '.join(repr(known) for known in FLAVORS)} (got {flavor!r})"
        )
    return True


# -- process modes ---------------------------------------------------------------


def _mode_label(process: str, mode: dict, index: int) -> str:
    # A mode's `id` is optional in the document (the runner fills it in later), and the
    # examples leave it out, so fall back to where it sits in the list.
    identifier = mode.get("id")
    if isinstance(identifier, str) and identifier:
        return f"process {process!r} mode {identifier!r}"
    return f"process {process!r} mode #{index}"


def _validate_processes(
    workflow: dict, environment: dict, devices: _NodeIndex, errors: list, warnings: list
) -> None:
    wf_procs = workflow.get("processes") or {}
    env_procs = environment.get("processes") or {}

    for name, eproc in env_procs.items():
        if not isinstance(eproc, dict):
            continue
        # (1) the workflow's own script process for this name (if any).
        wf_has_script = isinstance((wf_procs.get(name) or {}).get("script"), dict)
        # (2) modes carrying an env x-labcode.script (collected while shape-checking).
        modes_with_script: list = []

        for index, mode in enumerate(eproc.get("modes") or []):
            if not isinstance(mode, dict):
                continue
            extension = mode.get(EXTENSION_KEY)
            if extension is None:
                continue
            label = _mode_label(name, mode, index)
            if not isinstance(extension, dict):
                errors.append(f"{label}: x-labcode must be a mapping")
                continue
            errors.extend(
                f"{label}: {message}"
                for message in unknown_key_messages(extension, "x-labcode", SCRIPT_SITE_KEYS)
            )
            script = extension.get("script")
            if script is None:
                continue  # an x-labcode with no script; nothing further to check
            if not _validate_script(script, label, errors):
                continue
            modes_with_script.append(mode.get("id"))
            if script_flavor(script) == FLAVOR_SILA2:
                _check_mode_connection(mode, label, devices, errors)
                _check_reserved_locals(wf_procs.get(name), label, errors)

        # (1)/(2) exclusivity.
        if wf_has_script and modes_with_script:
            errors.append(
                f"process {name!r} has both a workflow script (1) and an env "
                f"x-labcode.script (2); they are mutually exclusive"
            )
        # typed-default reachability -- a warning, not an error.
        if not wf_has_script and not modes_with_script:
            warnings.append(
                f"process {name!r} has no script (workflow or x-labcode); its operations "
                f"will run as a typed-default no-op"
            )


def _check_reserved_locals(wf_process: Any, label: str, errors: list) -> None:
    """A `sila2` script finds its clients under reserved names, and a script's inputs are
    bound as its function's parameters -- so an input port of the same name would be
    silently overwritten by a client. Reject that, as `_id` is rejected (§4.1).

    Best effort: it needs the workflow's declaration of the process. A caller that passes
    an unexpanded document (``$import`` still unresolved) may not have it, in which case
    there is nothing to check here."""
    if not isinstance(wf_process, dict):
        return
    inputs = wf_process.get("inputs")
    if not isinstance(inputs, dict):
        return
    for name in RESERVED_LOCALS:
        if name in inputs:
            errors.append(
                f"{label}: the process declares an input port {name!r}, which a "
                f"'sila2' script's client would overwrite; labcode reserves "
                f"{', '.join(repr(local) for local in RESERVED_LOCALS)} in such a script"
            )


def _check_mode_connection(mode: dict, label: str, devices: _NodeIndex, errors: list) -> None:
    """A `sila2` script is handed a client, so at least one of the mode's devices has to
    say where that client connects."""
    listed = mode.get("devices")
    identifiers = [d for d in listed if isinstance(d, str)] if isinstance(listed, list) else []
    prefix = f"{label}: x-labcode.script.flavor 'sila2' but"
    if not identifiers:
        errors.append(f"{prefix} the mode lists no devices to connect to")
        return
    if any(identifier in devices.with_connection for identifier in identifiers):
        return
    undeclared = sorted(set(identifiers) - devices.declared)
    if undeclared:
        errors.append(f"{prefix} its device(s) {undeclared} are not declared in devices[]")
    else:
        errors.append(
            f"{prefix} none of its devices {identifiers} declares an x-labcode.connection"
        )


# -- transport routes -------------------------------------------------------------


def _replenishment_label(entry: dict) -> str:
    return (
        f"replenishment {entry.get('replenisher')!r} -> "
        f"{entry.get('device')!r}"
    )


def _validate_replenishments(environment: dict, errors: list, warnings: list) -> None:
    """Validate the ``x-labcode.script`` on each environment ``replenishments[]`` route.

    A refill script is side-effect only, like a transport's: no ports, no outputs. What
    it is handed is the pair -- which replenisher, which device, and the `amounts` the
    scheduler derived -- and it is expected to put that in.

    A route with no script runs as a timed no-op: the two machines are held for the
    declared duration and nothing is dispatched. That is a legitimate thing to write (an
    operator tops the stock up while the schedule waits for them), but it is easy to
    write by accident, so it is warned about exactly as a scriptless transport is.

    `flavor: sila2` is **refused** here for now. A sila2 script is handed clients, and
    which machine's clients a refill should get is a real question -- the replenisher's,
    or both ends' as a transport may ask for -- that this version does not answer. An
    error says so; silently running the script without clients would not.
    """
    for entry in environment.get("replenishments") or []:
        if not isinstance(entry, dict):
            continue
        label = _replenishment_label(entry)
        extension = entry.get(EXTENSION_KEY)
        if extension is None:
            warnings.append(
                f"{label}: no x-labcode.script; the refill will hold both machines for "
                f"its duration and command nothing"
            )
            continue
        if not isinstance(extension, dict):
            errors.append(f"{label}: x-labcode must be a mapping")
            continue
        errors.extend(
            f"{label}: {message}"
            for message in unknown_key_messages(extension, "x-labcode", SCRIPT_SITE_KEYS)
        )
        script = extension.get("script")
        if script is None:
            continue
        if not _validate_script(script, label, errors, SCRIPT_KEYS):
            continue
        if script_flavor(script) == FLAVOR_SILA2:
            errors.append(
                f"{label}: x-labcode.script.flavor 'sila2' is not supported for a refill "
                f"yet -- which machine's clients it should receive is not settled; "
                f"use 'python'"
            )


def _transport_label(transport: dict) -> str:
    return (
        f"transport {transport.get('transporter')!r} "
        f"{transport.get('from')} -> {transport.get('to')}"
    )


def _validate_transports(
    environment: dict,
    transporters: _NodeIndex,
    devices: _NodeIndex,
    errors: list,
    warnings: list,
) -> None:
    """Validate the ``x-labcode.script`` on each environment ``transports[]`` route (a
    transport script is side-effect only -- no ports, no outputs -- so only its shape, its
    connection and its `endpoints` request are checked). A real move (from != to) with no
    script runs as a bookkeeping-only no-op move (no device command), which is warned."""
    for transport in environment.get("transports") or []:
        if not isinstance(transport, dict):
            continue
        label = _transport_label(transport)
        extension = transport.get(EXTENSION_KEY)
        if extension is None:
            if transport.get("from") != transport.get("to"):  # a real move
                warnings.append(f"{label}: no x-labcode.script; it will run as a no-op move")
            continue
        if not isinstance(extension, dict):
            errors.append(f"{label}: x-labcode must be a mapping")
            continue
        errors.extend(
            f"{label}: {message}"
            for message in unknown_key_messages(extension, "x-labcode", SCRIPT_SITE_KEYS)
        )
        script = extension.get("script")
        if script is None:
            continue
        if not _validate_script(script, label, errors, TRANSPORT_SCRIPT_KEYS):
            continue
        _check_transport_endpoints(transport, script, label, devices, errors, warnings)
        if script_flavor(script) == FLAVOR_SILA2:
            _check_transport_connection(transport, label, transporters, errors)


def _check_transport_endpoints(
    transport: dict, script: dict, label: str, devices: _NodeIndex,
    errors: list, warnings: list,
) -> None:
    """Check a route's ``endpoints`` request: that it is a boolean, that the script can
    actually receive clients, and that there is a client to receive.

    Asking a `raw` script for endpoint clients is an **error**: a raw script is handed no
    clients at all (§1.6), so the request cannot be honoured and the author expects
    something that will not happen. Asking when neither end has an address is a **warning**
    -- the route still works through its transporter, and an environment written before its
    instruments have addresses is a legitimate intermediate state (as with `probe`)."""
    endpoints = script.get("endpoints")
    prefix = f"{label}: x-labcode.script.endpoints"
    if endpoints is None:
        return
    if not isinstance(endpoints, bool):
        errors.append(f"{prefix} must be a boolean (got {endpoints!r})")
        return
    if not endpoints:  # an explicit false states the default; nothing more to say
        return
    if script_flavor(script) != FLAVOR_SILA2:
        errors.append(
            f"{prefix} is true, but the script's flavor is "
            f"{script_flavor(script)!r}: only a 'sila2' script is handed clients (§1.6)"
        )
        return
    ends = [spot_device(transport.get(end)) for end in ("from", "to")]
    named = [device for device in ends if device is not None]
    if not any(device in devices.with_connection for device in named):
        warnings.append(
            f"{prefix} is true, but neither end of the route ({', '.join(named)}) declares "
            f"an x-labcode.connection; the script will be given its transporter only"
        )


def _check_transport_connection(
    transport: dict, label: str, transporters: _NodeIndex, errors: list
) -> None:
    """A `sila2` transport script drives one machine -- the transporter that carries the
    move -- so that is where its connection has to be."""
    identifier = transport.get("transporter")
    prefix = f"{label}: x-labcode.script.flavor 'sila2' but"
    if not isinstance(identifier, str) or not identifier:
        errors.append(f"{prefix} the route names no transporter")
    elif identifier not in transporters.declared:
        errors.append(f"{prefix} transporter {identifier!r} is not declared in transporters[]")
    elif identifier not in transporters.with_connection:
        errors.append(f"{prefix} transporter {identifier!r} declares no x-labcode.connection")
