"""The `sila2` script flavor: opening the connections a command script does not.

A `flavor: sila2` script (SPECIFICATIONS.md §1.1) is the *commands alone* -- it expects a
client to be there already. This module is the other half: it holds the connections open
for the duration of one operation, and it generates the few lines that put them in the
script's scope.

The split follows from where the code runs. A live SiLA2 client cannot cross the JSON
boundary between the runner and a child process, so the client cannot be handed down: it
has to be built *inside* the child. What the parent can hand down is text, so
`wrap` returns the script with a `with session(...)` around it and the child -- which knows
nothing about SiLA2 -- simply runs that.

The connection policy is *per operation*: connect at the start, disconnect at the end, no
pooling and no reconnection. It is a trust-based policy, and it costs a real connection per
op (a SiLA2 client fetches every feature definition when it is built) -- which the coarse
labcode cadence absorbs, and which is what a script that connects for itself already pays.

An operation holds every machine its activity occupies, and it may hold one it cannot reach
-- a plain holding device with no address. Those are not connected to, but they are not
hidden either: indexing one in `CLIENTS_LOCAL` yields a stand-in that says why there is no
client (`_Unconnected`), while an id the operation does not hold at all fails immediately as
the typo it is (`_Clients`).

``sila2`` itself is imported **inside** `connect`, not at module scope: labcode has to keep
working in an interpreter that never installed the extra (only a child running a `sila2`
script needs it). The absolute import means ``sila2`` here is the real distribution, not
this module.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from typing import Any

from ofplang.run.simulator import DeviceComputationError

from labcode.extension import CLIENT_LOCAL, CLIENTS_LOCAL, TLS_UNSUPPORTED, Connection

#: One machine to connect to: ``(id, host, port, insecure)``. The id is the environment's
#: device / transporter id -- the key the script looks a client up by.
Target = tuple[str, str, int, bool]

#: Why an operation holds a machine but opens no client to it. The key is what `wrap` writes
#: into the generated code; the value is the ``(error code, explanation)`` a script gets if
#: it uses the machine anyway. Keys are kept short because they travel as literals.
NO_CONNECTION = "no_connection"
NOT_REQUESTED = "not_requested"
UNAVAILABLE_REASONS: dict[str, tuple[str, str]] = {
    NO_CONNECTION: ("sila2_not_connected", "it declares no x-labcode.connection"),
    NOT_REQUESTED: (
        "sila2_endpoints_not_requested",
        "this transport does not ask for the clients of the devices at either end of its "
        "route; add `endpoints: true` to its x-labcode.script",
    ),
}
#: For a reason this version does not know: report it as-is rather than losing it.
UNAVAILABLE_FALLBACK_CODE = "sila2_not_connected"


def _unavailable_reason(reason: str) -> tuple[str, str]:
    return UNAVAILABLE_REASONS.get(reason, (UNAVAILABLE_FALLBACK_CODE, reason))


class _Unconnected:
    """Stands in for a machine the operation holds but has no client for.

    Two things it must do, neither of which a `KeyError` on an id can. It is **falsy**, so
    ``if sila2_clients[some_id]:`` reads as "is there a client for it" -- a script that can
    work either way needs no knowledge of how the absence is represented. And using it
    anyway **fails the operation with why there is no client**, which is a fact about the
    environment (a device with no address) that no amount of reading the script reveals."""

    def __init__(self, identifier: str, reason: str) -> None:
        self._identifier = identifier
        self._reason = reason

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        _code, explanation = _unavailable_reason(self._reason)
        return f"<no SiLA2 client for {self._identifier!r}: {explanation}>"

    def __getattr__(self, name: str) -> Any:
        # A dunder lookup comes from generic machinery (copy, pickle, a test runner's
        # introspection), never from a script commanding an instrument, and such machinery
        # relies on `AttributeError` to fall back. Only a real attempt to drive the machine
        # is worth failing the operation over.
        if name.startswith("__"):
            raise AttributeError(name)
        code, explanation = _unavailable_reason(self._reason)
        raise DeviceComputationError(
            f"labcode: no SiLA2 client for {self._identifier!r}: {explanation}", code=code
        )


class _Clients(dict):
    """`CLIENTS_LOCAL`: the clients this operation opened, by machine id, in the order they
    were opened.

    An ordinary mapping -- ``in``, ``.get()``, iteration and equality are the dict's -- with
    one addition: indexing a machine the operation *holds* without a client yields an
    `_Unconnected` explaining itself, while indexing an id the operation does not hold at all
    raises, naming what it does hold. The distinction is the point: the first is a fact about
    the environment a script may legitimately have to handle, the second is a typo, and
    turning a typo into a falsy object would let it survive until something odd happened
    later."""

    def __init__(self, unavailable: Mapping[str, str]) -> None:
        super().__init__()
        self._unavailable = dict(unavailable)

    def __missing__(self, key: Any) -> Any:
        reason = self._unavailable.get(key) if isinstance(key, str) else None
        if reason is None:
            raise KeyError(
                f"labcode: {key!r} is not a machine this operation holds "
                f"(it holds {sorted([*self, *self._unavailable])})"
            )
        return _Unconnected(key, reason)


def connect(host: str, port: int, *, insecure: bool = False) -> Any:
    """Open a SiLA2 client to ``host:port``.

    Raises `DeviceComputationError` -- a graceful operation failure -- when the client
    library is missing or the connection would need TLS. The TLS refusal is the same rule
    the dialect front door applies (§1.4); it is repeated here because this function is
    also reachable without one (a script may call it directly)."""
    if not insecure:
        raise DeviceComputationError(
            f"cannot connect to {host}:{port}: {TLS_UNSUPPORTED}",
            code="sila2_tls_unsupported",
        )
    try:
        from sila2.client import SilaClient  # noqa: PLC0415 - deliberately a local import
    except ImportError as exc:
        raise DeviceComputationError(
            "the SiLA2 client library is not importable by the interpreter running this "
            "script; install it there (`uv sync --extra sila2`, or "
            "`pip install 'labcode[sila2]'`)",
            code="sila2_unavailable",
        ) from exc
    return SilaClient(host, port, insecure=True)


@contextmanager
def session(
    targets: Sequence[Target], *, unavailable: Mapping[str, str] | None = None
) -> Iterator[tuple[dict[str, Any], Any]]:
    """Hold a client open to each of `targets` for the duration of one operation.

    Yields ``(clients, client)``: the clients by id in `targets` order, and the first of
    them (the alias a single-machine operation uses). Every client that was opened is closed
    on the way out -- whether the body returned, the body raised, or a *later* connection
    failed -- because each one's close is registered the moment it opens.

    A failure to connect is a graceful operation failure naming the machine, so a lab that
    is not reachable reads as "this op could not talk to that instrument" rather than as a
    library traceback.

    `unavailable` is ``{id: reason}`` for the machines the operation holds but opens no
    client to (`UNAVAILABLE_REASONS`); they are absent from the mapping but explain
    themselves when indexed (`_Clients`)."""
    if not targets:  # the validator rejects this; a direct caller might still do it
        raise DeviceComputationError(
            "a sila2 session needs at least one connection to open", code="sila2_no_target"
        )
    clients: dict[str, Any] = _Clients(unavailable or {})
    with ExitStack() as stack:
        for identifier, host, port, insecure in targets:
            try:
                client = connect(host, port, insecure=insecure)
            except Exception as exc:
                code = getattr(exc, "code", None) or "sila2_connect_failed"
                raise DeviceComputationError(
                    f"cannot reach {identifier!r} at {host}:{port}: {exc}", code=code
                ) from exc
            # Registered before the next connection is attempted, so a failure there
            # still closes this one.
            stack.callback(_close_quietly, client)
            clients[identifier] = client
        yield clients, next(iter(clients.values()))


def _close_quietly(client: Any) -> None:
    """Close `client`, ignoring a failure to do so: an operation's outcome is what the
    script computed, and a channel that would not shut down cleanly must not replace it."""
    with suppress(Exception):
        client.close()


# -- generating the wrapper ------------------------------------------------------------


def wrap(
    code: str,
    targets: Sequence[tuple[str, Connection]],
    *,
    unavailable: Mapping[str, str] | None = None,
) -> str:
    """`code` (a `flavor: sila2` script body) with its connections opened around it.

    The result is a function body like any other script -- the child runs it unchanged --
    with `CLIENTS_LOCAL` and `CLIENT_LOCAL` bound for the script to use. An empty body
    falls back to ``pass`` so the ``with`` still compiles.

    `unavailable` (see `session`) is written into the call when there is any, and left out
    entirely when there is not -- an operation whose every machine is reachable generates
    exactly what it always did.

    Connections are the only thing bound here. `labcode.sila2_commands.settle` is a helper a
    script may want, but it arrives by an ordinary ``import`` written in the script, not by
    injection: a name that appears out of nowhere is worth spending only on what a script
    cannot obtain for itself, and a connection is that; an import is not."""
    literals = ",\n".join(
        f"    ({identifier!r}, {connection.host!r}, {connection.port}, "
        f"{connection.insecure!r})"
        for identifier, connection in targets
    )
    held = ""
    if unavailable:
        pairs = ", ".join(
            f"{identifier!r}: {reason!r}" for identifier, reason in unavailable.items()
        )
        held = f", unavailable={{{pairs}}}"
    body = textwrap.indent(code, "    ").rstrip() or "    pass"
    return (
        "from labcode.sila2 import session as __lc_session\n"
        f"with __lc_session([\n{literals},\n]{held}) as ({CLIENTS_LOCAL}, {CLIENT_LOCAL}):\n"
        f"{body}\n"
    )


def failing_code(reason: str) -> str:
    """A script body that fails its operation with `reason`.

    Used where a `sila2` script cannot be run at all (no connection to open). It is
    returned as *code* rather than raised, because a code resolver runs inside dispatch,
    where an exception would escape the run rather than fail one operation."""
    return f"raise RuntimeError({reason!r})\n"


def plan_clients(
    machines: Iterable[tuple[Any, Mapping[str, Connection] | None]],
) -> tuple[list[tuple[str, Connection]], dict[str, str]]:
    """Split the machines an operation holds into the ones to open a client to and the ones
    there is no client for.

    `machines` is ``(id, where to look that id up)`` in the order the operation holds them --
    a mode's ``devices[]``, or a transport's transporter followed by the devices at either
    end of its route. Each id is looked up in **its own** map, so a device and a transporter
    that happen to share an id cannot shadow each other, and the first mention of an id wins,
    so a machine held twice is connected to once. A map of ``None`` says the operation holds
    that machine but is **not asking to drive it** (a transport without `endpoints`), which
    is a different fact from having no address for it, and is reported as one.

    Returns ``(targets, unavailable)``: what to connect, in that order, and ``{id: reason}``
    for the rest. Holding a machine there is no client for is **not** an error -- an
    operation may occupy a device it does not drive -- so this is a filter, not a
    requirement; what an empty `targets` means is the caller's to decide."""
    targets: list[tuple[str, Connection]] = []
    unavailable: dict[str, str] = {}
    seen: set[str] = set()
    for identifier, connections in machines:
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            continue
        seen.add(identifier)
        if connections is None:
            unavailable[identifier] = NOT_REQUESTED
            continue
        connection = connections.get(identifier)
        if connection is None:
            unavailable[identifier] = NO_CONNECTION
        else:
            targets.append((identifier, connection))
    return targets, unavailable
