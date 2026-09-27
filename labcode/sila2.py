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
client (`labcode.connections.Unconnected`), while an id the operation does not hold at all fails
immediately as the typo it is (`labcode.connections.Clients`). That machinery, and the
holding of every client for one operation, is shared with the LADS flavor; this module adds
only what is SiLA2 -- how one client is opened.

``sila2`` itself is imported **inside** `connect`, not at module scope: labcode has to keep
working in an interpreter that never installed the extra (only a child running a `sila2`
script needs it). The absolute import means ``sila2`` here is the real distribution, not
this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from ofplang.run.simulator import DeviceComputationError

from labcode import connections
from labcode.connections import NO_CONNECTION, NOT_REQUESTED, failing_code
from labcode.extension import (
    CLIENT_LOCAL,
    CLIENTS_LOCAL,
    CONNECTION_KIND,
    TLS_UNSUPPORTED,
    Connection,
)

__all__ = [
    "NOT_REQUESTED",
    "NO_CONNECTION",
    "SILA2",
    "Target",
    "connect",
    "failing_code",
    "plan_clients",
    "session",
    "wrap",
]

#: How the shared machinery (`labcode.connections`) names this protocol in messages and codes.
SILA2 = connections.Protocol(label="SiLA2", prefix="sila2")

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


def session(
    targets: Sequence[Target], *, unavailable: Mapping[str, str] | None = None
) -> AbstractContextManager[tuple[dict[str, Any], Any]]:
    """Hold a client open to each of `targets` for the duration of one operation.

    Yields ``(clients, client)``: the clients by id in `targets` order, and the first of
    them (the alias a single-machine operation uses). Every client that was opened is closed
    on the way out -- whether the body returned, the body raised, or a *later* connection
    failed (`labcode.connections.open_clients`).

    `unavailable` is ``{id: reason}`` for the machines the operation holds but opens no
    client to; they are absent from the mapping but explain themselves when indexed.

    `connect` is looked up at call time, through this module, so replacing
    ``labcode.sila2.connect`` replaces what a session opens with."""
    return connections.open_clients(
        targets,
        protocol=SILA2,
        connect=lambda host, port, insecure: connect(host, port, insecure=insecure),
        close=lambda client: client.close(),
        unavailable=unavailable,
    )


def wrap(
    code: str,
    targets: Sequence[tuple[str, Connection]],
    *,
    unavailable: Mapping[str, str] | None = None,
) -> str:
    """`code` (a `flavor: sila2` script body) with its connections opened around it.

    The result is a function body like any other script -- the child runs it unchanged --
    with `CLIENTS_LOCAL` and `CLIENT_LOCAL` bound for the script to use.

    Connections are the only thing bound here. `labcode.sila2_commands.settle` is a helper a
    script may want, but it arrives by an ordinary ``import`` written in the script, not by
    injection: a name that appears out of nowhere is worth spending only on what a script
    cannot obtain for itself, and a connection is that; an import is not."""
    literals = [
        f"({identifier!r}, {connection.host!r}, {connection.port}, {connection.insecure!r})"
        for identifier, connection in targets
    ]
    return connections.render_wrapper(
        code,
        module="labcode.sila2",
        clients_local=CLIENTS_LOCAL,
        client_local=CLIENT_LOCAL,
        literals=literals,
        unavailable=unavailable,
    )


def plan_clients(
    machines: Iterable[tuple[Any, Mapping[str, Connection] | None]],
) -> tuple[list[tuple[str, Connection]], dict[str, str]]:
    """Split the machines an operation holds into the ones to open a SiLA2 client to and the
    ones there is no client for (`labcode.connections.plan_clients`); a machine whose
    connection is for another protocol is one of the latter."""
    return connections.plan_clients(machines, kind=CONNECTION_KIND)
