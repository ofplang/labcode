"""labcode dialect front-door validation of the ``x-labcode`` extension (P5).

`ofplang-validate` checks a workflow is portable v0; `ofplang-schedule` merely tolerates
``x-`` extension keys in an environment (it neither interprets nor checks them -- at *any*
position, root and process included). labcode *owns* the ``x-labcode`` extension, so this
module is its conformance validator -- the reference implementation of the labcode dialect
spec (`SPECIFICATIONS.md`). `lc run` calls it at the front door, after the shared workflow
front door, over the workflow (needed for the (1)/(2) exclusivity rule) and the environment
(where ``x-labcode`` lives).

Three kinds of check:

* **position** -- an ``x-labcode`` outside the four places this version reads (a process
  mode, a transport route, a device, a transporter) is an error. Nothing else would ever
  read it, and `ofplang-schedule` will not complain either, so a misplaced block would
  otherwise be silent for good. (An ``x-labcode`` in the *workflow* is deliberately not
  checked: that document is portable v0, read by other implementations, and policing
  other people's extension keys there is not labcode's business.)
* **shape** -- the keys are closed: an unknown key is an error rather than something
  ignored, so a misspelled ``flavour:`` or a ``probe:`` this version cannot honour is
  reported instead of quietly doing nothing.
* **relational / runnability** --
  - **(1)/(2) exclusive**: a process must not carry both a workflow ``script`` (1) and an
    env ``x-labcode.script`` (2); that is ambiguous.
  - **a `sila2` script needs somewhere to connect**: its mode must have a device (or its
    route a transporter) that declares an ``x-labcode.connection``.
  - **typed-default reachability** (a *warning*, not an error): a process with neither a
    workflow script nor any mode's ``x-labcode.script`` will run as a typed-default no-op
    (a device not yet scripted). Allowed -- convenient while mocking -- but surfaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from labcode.extension import (
    EXTENSION_KEY,
    FLAVOR_SILA2,
    FLAVORS,
    NODE_KEYS,
    SCRIPT_KEYS,
    SCRIPT_SITE_KEYS,
    TLS_INSECURE_NOT_DECLARED,
    TLS_UNSUPPORTED,
    find_extensions,
    format_path,
    is_supported_position,
    parse_connection,
    script_flavor,
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
    devices = _node_index(environment, "devices")
    transporters = _node_index(environment, "transporters")
    _validate_nodes(environment, "devices", "device", errors)
    _validate_nodes(environment, "transporters", "transporter", errors)
    _validate_processes(workflow, environment, devices, errors, warnings)
    _validate_transports(environment, transporters, errors, warnings)

    return DialectResult(ok=not errors, errors=errors, warnings=warnings)


# -- position ------------------------------------------------------------------


_BELONGS = (
    "it belongs on a process mode (processes.<p>.modes[]), a transport route "
    "(transports[]), a device (devices[]) or a transporter (transporters[])"
)


def _validate_positions(environment: dict, errors: list) -> None:
    """Reject an ``x-labcode`` where nothing reads one. The environment root is called out
    by name because it is the natural guess for a document-wide default block -- which is
    exactly what a later version plans to put there."""
    for path, _value in find_extensions(environment):
        if is_supported_position(path):
            continue
        if path:
            errors.append(f"x-labcode at {format_path(path)} is not supported; {_BELONGS}")
        else:
            errors.append(
                "x-labcode at the environment root is not supported in this version "
                "(a document-wide default block for availability probing is planned); "
                f"{_BELONGS}"
            )


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


def _validate_nodes(environment: dict, key: str, kind: str, errors: list) -> None:
    """Shape-check the ``x-labcode`` on each ``devices[]`` / ``transporters[]`` entry: a
    `connection` and nothing else, and -- while TLS has nowhere to keep its credentials --
    an insecure one."""
    entries = environment.get(key)
    if not isinstance(entries, list):
        return
    connected: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        extension = entry.get(EXTENSION_KEY)
        if extension is None:
            continue
        identifier = entry.get("id")
        label = f"{kind} {identifier!r}" if isinstance(identifier, str) else f"{kind} #{index}"
        if not isinstance(extension, dict):
            errors.append(f"{label}: x-labcode must be a mapping")
            continue
        errors.extend(
            f"{label}: {message}"
            for message in unknown_key_messages(extension, "x-labcode", NODE_KEYS)
        )
        raw = extension.get("connection")
        if raw is None:
            continue
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


# -- scripts --------------------------------------------------------------------


def _validate_script(script: Any, label: str, errors: list, warnings: list) -> bool:
    """Shape-check an ``x-labcode.script``. Returns False if it is not a mapping at all
    (nothing further can be said about it)."""
    if not isinstance(script, dict):
        errors.append(f"{label}: x-labcode.script must be a mapping")
        return False
    errors.extend(
        f"{label}: {message}"
        for message in unknown_key_messages(script, "x-labcode.script", SCRIPT_KEYS)
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
    elif script_flavor(script) == FLAVOR_SILA2:
        # The schema accepts `sila2` already so an environment can be written against it,
        # but nothing wraps the code yet: say so rather than let it run as if it were raw
        # and fail on an undefined `client`.
        warnings.append(
            f"{label}: x-labcode.script.flavor 'sila2' is not interpreted in this version; "
            f"the code runs as written (raw)"
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
            if not _validate_script(script, label, errors, warnings):
                continue
            modes_with_script.append(mode.get("id"))
            if script_flavor(script) == FLAVOR_SILA2:
                _check_mode_connection(mode, label, devices, errors)

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


def _transport_label(transport: dict) -> str:
    return (
        f"transport {transport.get('transporter')!r} "
        f"{transport.get('from')} -> {transport.get('to')}"
    )


def _validate_transports(
    environment: dict, transporters: _NodeIndex, errors: list, warnings: list
) -> None:
    """Validate the ``x-labcode.script`` on each environment ``transports[]`` route (a
    transport script is side-effect only -- no ports, no outputs -- so only its shape and
    its connection are checked). A real move (from != to) with no script runs as a
    bookkeeping-only no-op move (no device command), which is warned."""
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
        if not _validate_script(script, label, errors, warnings):
            continue
        if script_flavor(script) == FLAVOR_SILA2:
            _check_transport_connection(transport, label, transporters, errors)


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
