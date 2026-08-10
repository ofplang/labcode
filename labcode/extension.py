"""Reading the ``x-labcode`` extension out of a raw environment document.

Two modules act on the extension: `labcode.dialect` validates it at the `lc run` front
door, and `labcode.backend` executes what it says. They must agree on the same three
questions -- *where* an ``x-labcode`` may appear, *which flavor* a script is written in,
and *how to reach* a device -- so the answers live here once rather than drifting apart in
two places.

Everything here reads the **raw** environment dict (the one that still carries the ``x-``
keys, unlike the parsed `Environment`), and nothing here reports diagnostics on its own:
`parse_connection` returns messages for its caller to label, and the lookups are
best-effort (they skip what the validator rejects).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

#: The extension key labcode owns. Only this exact key is ours -- ``x-anything-else``
#: belongs to somebody else's extension and is none of our business.
EXTENSION_KEY = "x-labcode"

# -- script -------------------------------------------------------------------

#: A script's ``flavor``: how the ``code`` is meant to be run. ``raw`` (the default) is
#: the whole function body, written by its author; ``sila2`` is the command body alone,
#: with the SiLA2 client(s) supplied around it.
FLAVOR_RAW = "raw"
FLAVOR_SILA2 = "sila2"
FLAVORS: tuple[str, ...] = (FLAVOR_RAW, FLAVOR_SILA2)

#: Closed key sets. An unknown key is a typo (or a feature this version does not have),
#: and silently ignoring it is how a misspelled `flavour:` becomes a mystery at run time.
SCRIPT_KEYS: tuple[str, ...] = ("language", "code", "flavor")
#: `x-labcode` on a process mode or a transport route -- the places a script lives.
SCRIPT_SITE_KEYS: tuple[str, ...] = ("script",)
#: `x-labcode` on a device or a transporter -- how to reach it, and whether to check.
NODE_KEYS: tuple[str, ...] = ("connection", "probe")
#: `x-labcode` at the environment root -- document-wide defaults, and nothing else.
ROOT_KEYS: tuple[str, ...] = ("probe",)
CONNECTION_KEYS: tuple[str, ...] = ("kind", "host", "port", "insecure")
PROBE_KEYS: tuple[str, ...] = ("enabled", "timeout", "interval")


#: The names a `sila2` script finds in its scope (the clients by device id, and the first
#: of them). They are *reserved*: an input port of the same name would be silently
#: overwritten by them, since a script's inputs are bound as its function's parameters.
#:
#: Connections are the only thing injected, so this list is the only thing reserved. The
#: `labcode.sila2_commands` helpers reach a script by an ordinary ``import`` it writes
#: itself, which needs no name here and takes none away from the author.
CLIENTS_LOCAL = "sila2_clients"
CLIENT_LOCAL = "sila2_client"
RESERVED_LOCALS: tuple[str, ...] = (CLIENTS_LOCAL, CLIENT_LOCAL)


def script_flavor(script: Any) -> str:
    """The `flavor` of an ``x-labcode.script`` mapping; `FLAVOR_RAW` when it declares
    none. A malformed value reads as raw here -- the dialect validator rejects it at the
    front door, so it never reaches execution."""
    if isinstance(script, dict):
        flavor = script.get("flavor")
        if isinstance(flavor, str):
            return flavor
    return FLAVOR_RAW


def python_code(script: Any) -> str | None:
    """The `code` of a python ``x-labcode.script`` mapping, or None (absent / not python).

    A non-python script reads as absent so resolution falls through to the next source;
    the dialect validator rejects it at the front door, so it is never silently mis-run."""
    if isinstance(script, dict) and script.get("language") == "python":
        code = script.get("code")
        return code if isinstance(code, str) else ""
    return None


# -- connection ---------------------------------------------------------------

CONNECTION_KIND = "sila2"
DEFAULT_INSECURE = False
MAX_PORT = 65535

#: TLS needs credentials (a root certificate, at least) and the schema has nowhere to put
#: them, so this version can only speak to a server without it. That is a real limitation
#: and it is reported at the front door rather than as a connection failure mid-run. The
#: default stays `false` (the safe side) so adding TLS later is a pure addition: new
#: fields, and these two errors go away.
TLS_UNSUPPORTED = "connection.insecure must be true: TLS is not supported in this version"
TLS_INSECURE_NOT_DECLARED = (
    "connection.insecure is not declared, so it defaults to false (TLS), which is not "
    "supported in this version; declare `insecure: true`"
)


@dataclass(frozen=True)
class Connection:
    """How to reach one device or transporter: a validated ``x-labcode.connection``."""

    host: str
    port: int
    insecure: bool = DEFAULT_INSECURE
    kind: str = CONNECTION_KIND


def parse_connection(raw: Any) -> tuple[Connection | None, list[str]]:
    """Parse an ``x-labcode.connection`` mapping into a `Connection`.

    Returns ``(connection, errors)`` -- the connection is None iff there are errors. The
    messages name the offending field (``connection.host must be ...``) but not *whose*
    connection it is: the caller knows the device and prefixes them."""
    if not isinstance(raw, dict):
        return None, ["connection must be a mapping"]

    errors = unknown_key_messages(raw, "connection", CONNECTION_KEYS)

    kind = raw.get("kind", CONNECTION_KIND)
    if kind != CONNECTION_KIND:
        errors.append(f"connection.kind must be {CONNECTION_KIND!r} (got {kind!r})")
    host = raw.get("host")
    if not isinstance(host, str) or not host.strip():
        errors.append("connection.host must be a non-empty string")
    port = raw.get("port")
    # `bool` is an `int` in Python, so `port: true` would otherwise pass as port 1.
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= MAX_PORT:
        errors.append(f"connection.port must be an integer in 1..{MAX_PORT}")
    insecure = raw.get("insecure", DEFAULT_INSECURE)
    if not isinstance(insecure, bool):
        errors.append("connection.insecure must be a boolean")

    if errors:
        return None, errors
    # Every field was type-checked above; the casts say so to a type checker, which cannot
    # see that through the error list.
    connection = Connection(
        host=cast(str, host),
        port=cast(int, port),
        insecure=cast(bool, insecure),
        kind=cast(str, kind),
    )
    return connection, []


# -- probe ---------------------------------------------------------------------

#: Probing is **off unless asked for**: writing a policy says *how* to probe, never
#: *whether* to. The two are independent, so adding an `interval` to an environment
#: cannot start probing something that was not being probed before.
DEFAULT_PROBE_ENABLED = False
DEFAULT_PROBE_TIMEOUT = 5.0
#: `interval: once` -- probe at the start of the run and keep that answer. Internally
#: it is `None`, so a policy's interval is either a number of seconds or "no repeat".
INTERVAL_ONCE = "once"


@dataclass(frozen=True)
class Probe:
    """A machine's availability-probing policy, with every field resolved.

    `timeout` and `interval` are **real seconds** -- probing is work done in the parent
    process against the real world, so it has nothing to do with the environment's time
    unit or the wall-clock pacing of the run. `interval` is None for `once`, 0 to probe
    on every replan (for a test or a diagnosis), or a number of seconds to re-probe on."""

    enabled: bool = DEFAULT_PROBE_ENABLED
    timeout: float = DEFAULT_PROBE_TIMEOUT
    interval: float | None = None


def parse_probe(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Parse an ``x-labcode.probe`` mapping.

    Returns ``(declared fields, errors)`` -- only the fields the mapping actually
    declares, so a root policy and a machine's own can be merged field by field
    (`merge_probe`), with `interval` normalised to None for `once`. Messages name the
    field but not the machine: the caller knows which one it is reading."""
    if not isinstance(raw, dict):
        return {}, ["probe must be a mapping"]

    errors = unknown_key_messages(raw, "probe", PROBE_KEYS)
    declared: dict[str, Any] = {}

    if "enabled" in raw:
        enabled = raw["enabled"]
        if isinstance(enabled, bool):
            declared["enabled"] = enabled
        else:
            errors.append("probe.enabled must be a boolean")
    if "timeout" in raw:
        timeout = raw["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            errors.append("probe.timeout must be a positive number of seconds")
        else:
            declared["timeout"] = float(timeout)
    if "interval" in raw:
        interval = raw["interval"]
        if interval == INTERVAL_ONCE:
            declared["interval"] = None
        elif isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0:
            errors.append(
                f"probe.interval must be {INTERVAL_ONCE!r}, or a number of seconds "
                f">= 0 (0 = probe on every replan)"
            )
        else:
            declared["interval"] = float(interval)

    return declared, errors


def merge_probe(*layers: Mapping[str, Any]) -> Probe:
    """The effective policy from `layers`, outermost first: the environment root's
    defaults, then the machine's own. Later layers win **field by field**, so a machine
    can change one thing (an interval) without restating the rest."""
    fields: dict[str, Any] = {}
    for layer in layers:
        fields.update(layer)
    return Probe(**fields)


def declared_probe(extension: Any) -> dict[str, Any]:
    """The probe fields declared in one ``x-labcode`` mapping (empty when it declares
    none). Best-effort: a malformed policy reads as declaring nothing, since the dialect
    validator is what reports it."""
    if not isinstance(extension, dict):
        return {}
    raw = extension.get("probe")
    if raw is None:
        return {}
    declared, _errors = parse_probe(raw)
    return declared


def device_connections(environment: dict) -> dict[str, Connection]:
    """``{device id: Connection}`` for every ``devices[]`` entry that declares a valid
    connection, in document order (the order a mode's clients are built in)."""
    return _connections(environment.get("devices"))


def transporter_connections(environment: dict) -> dict[str, Connection]:
    """``{transporter id: Connection}``, as `device_connections`. Kept separate from the
    devices so that a device and a transporter sharing an id cannot shadow each other."""
    return _connections(environment.get("transporters"))


def _connections(entries: Any) -> dict[str, Connection]:
    found: dict[str, Connection] = {}
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        extension = entry.get(EXTENSION_KEY)
        if not isinstance(identifier, str) or not isinstance(extension, dict):
            continue
        raw = extension.get("connection")
        if raw is None:
            continue
        connection, _errors = parse_connection(raw)
        if connection is not None:  # malformed ones are the validator's to report
            found[identifier] = connection
    return found


# -- where an x-labcode may appear ---------------------------------------------


class _AnyElement:
    """Wildcard for one path element (a mapping key or a list index)."""

    def __repr__(self) -> str:  # pragma: no cover - a debugging aid
        return "*"


ANY = _AnyElement()

#: A path to the mapping that *holds* an ``x-labcode``: mapping keys and list indices,
#: outermost first. ``("devices", 0)`` is `environment["devices"][0]`; ``()`` is the root.
Path = tuple[Any, ...]

#: Where this version interprets an ``x-labcode``: the document root (probing defaults
#: for every machine) and the four places that describe one thing each. Anywhere else it
#: would be read by nobody, and a document whose author expected otherwise is better off
#: being told so -- see `is_supported_position`.
SUPPORTED_POSITIONS: tuple[Path, ...] = (
    (),
    ("processes", ANY, "modes", ANY),
    ("transports", ANY),
    ("devices", ANY),
    ("transporters", ANY),
)


def find_extensions(document: Any) -> list[tuple[Path, Any]]:
    """Every ``x-labcode`` in `document`, as ``(path to its holder, value)``.

    The walk does **not** descend into an ``x-labcode`` value: what is inside one is the
    shape check's business, and descending would report a nested key twice."""
    found: list[tuple[Path, Any]] = []
    _walk(document, (), found)
    return found


def _walk(node: Any, path: Path, found: list[tuple[Path, Any]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == EXTENSION_KEY:
                found.append((path, value))
                continue
            _walk(value, (*path, key), found)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, (*path, index), found)


def is_supported_position(path: Path) -> bool:
    """Is an ``x-labcode`` held at `path` one this version reads?"""
    return any(_matches(path, pattern) for pattern in SUPPORTED_POSITIONS)


def _matches(path: Path, pattern: Path) -> bool:
    if len(path) != len(pattern):
        return False
    return all(
        expected is ANY or expected == element
        for element, expected in zip(path, pattern, strict=True)
    )


def format_path(path: Path) -> str:
    """`path` as a readable locator: ``processes.seal.modes[0]``, ``devices[1]``, or
    ``<root>`` for the document itself."""
    if not path:
        return "<root>"
    text = ""
    for element in path:
        if isinstance(element, int):
            text += f"[{element}]"
        else:
            text += f".{element}" if text else str(element)
    return text


# -- shared diagnostics --------------------------------------------------------


def unknown_key_messages(mapping: dict, where: str, allowed: Sequence[str]) -> list[str]:
    """A message per key of `mapping` outside `allowed`, in document order. `where` names
    the mapping (``x-labcode``, ``x-labcode.script``, ``connection``)."""
    return [_unknown_key_message(where, key, allowed) for key in mapping if key not in allowed]


def _unknown_key_message(where: str, key: Any, allowed: Sequence[str]) -> str:
    return f"{where} has an unknown key {key!r} (allowed: {', '.join(sorted(allowed))})"
