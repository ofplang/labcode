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

``sila2`` itself is imported **inside** `connect`, not at module scope: labcode has to keep
working in an interpreter that never installed the extra (only a child running a `sila2`
script needs it). The absolute import means ``sila2`` here is the real distribution, not
this module.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from typing import Any

from ofplang.run.simulator import DeviceComputationError

from labcode.extension import CLIENT_LOCAL, CLIENTS_LOCAL, TLS_UNSUPPORTED, Connection

#: One machine to connect to: ``(id, host, port, insecure)``. The id is the environment's
#: device / transporter id -- the key the script looks a client up by.
Target = tuple[str, str, int, bool]


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
def session(targets: Sequence[Target]) -> Iterator[tuple[dict[str, Any], Any]]:
    """Hold a client open to each of `targets` for the duration of one operation.

    Yields ``(clients, client)``: the clients by id in `targets` order, and the first of
    them (the alias a single-device operation uses). Every client that was opened is closed
    on the way out -- whether the body returned, the body raised, or a *later* connection
    failed -- because each one's close is registered the moment it opens.

    A failure to connect is a graceful operation failure naming the machine, so a lab that
    is not reachable reads as "this op could not talk to that instrument" rather than as a
    library traceback."""
    if not targets:  # the validator rejects this; a direct caller might still do it
        raise DeviceComputationError(
            "a sila2 session needs at least one connection to open", code="sila2_no_target"
        )
    clients: dict[str, Any] = {}
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


def wrap(code: str, targets: Sequence[tuple[str, Connection]]) -> str:
    """`code` (a `flavor: sila2` script body) with its connections opened around it.

    The result is a function body like any other script -- the child runs it unchanged --
    with `CLIENTS_LOCAL` and `CLIENT_LOCAL` bound for the script to use. An empty body
    falls back to ``pass`` so the ``with`` still compiles.

    Connections are the only thing bound here. `labcode.sila2_commands.settle` is a helper a
    script may want, but it arrives by an ordinary ``import`` written in the script, not by
    injection: a name that appears out of nowhere is worth spending only on what a script
    cannot obtain for itself, and a connection is that; an import is not."""
    literals = ",\n".join(
        f"    ({identifier!r}, {connection.host!r}, {connection.port}, "
        f"{connection.insecure!r})"
        for identifier, connection in targets
    )
    body = textwrap.indent(code, "    ").rstrip() or "    pass"
    return (
        "from labcode.sila2 import session as __lc_session\n"
        f"with __lc_session([\n{literals},\n]) as ({CLIENTS_LOCAL}, {CLIENT_LOCAL}):\n"
        f"{body}\n"
    )


def failing_code(reason: str) -> str:
    """A script body that fails its operation with `reason`.

    Used where a `sila2` script cannot be run at all (no connection to open). It is
    returned as *code* rather than raised, because a code resolver runs inside dispatch,
    where an exception would escape the run rather than fail one operation."""
    return f"raise RuntimeError({reason!r})\n"


def targets_of(identifiers: Any, connections: Mapping[str, Connection]) -> list[
    tuple[str, Connection]
]:
    """The connectable machines among `identifiers`, in that order and without repeats.

    A device an operation occupies but cannot be reached at is simply not connected to (a
    mode may hold a device it does not drive), so `identifiers` is a filter, not a
    requirement -- except that everything being filtered out leaves nothing to connect."""
    found: list[tuple[str, Connection]] = []
    seen: set[str] = set()
    for identifier in identifiers if isinstance(identifiers, list) else []:
        if not isinstance(identifier, str) or identifier in seen:
            continue
        connection = connections.get(identifier)
        if connection is not None:
            seen.add(identifier)
            found.append((identifier, connection))
    return found
