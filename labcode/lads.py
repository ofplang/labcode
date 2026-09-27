"""The `lads` script flavor: opening the LADS OPC UA connections a command script does not.

The LADS counterpart of `labcode.sila2`, and the same shape (SPECIFICATIONS.md §1.10). A
`flavor: lads` script is the *commands alone* -- it expects its clients to be there already.
This module holds them open for one operation and generates the lines that put them in the
script's scope. The clients cannot cross the JSON boundary between the runner and a child
process, so they are built *inside* the child: `wrap` returns the script with a
``with session(...)`` around it, and the child -- which knows nothing of LADS -- runs that.

What a script is handed for each machine is a `labcode.lads_unit.LadsUnit`: one functional unit
of one device on the machine's LADS server, found by the connection's ``device`` / ``unit`` or,
where those are omitted, as the only one there is (G35).

Everything that is not LADS -- choosing which machines to connect to, the mapping that tells a
typo from a machine held without a client, holding every client for the operation and closing
each on any exit -- is shared with `labcode.sila2` through `labcode.connections`. So is the
policy: connect at the start of an operation, disconnect at the end, no pooling, no reconnection.

``asyncua`` is imported **inside** `connect`, not at module scope, so labcode keeps working in an
interpreter that never installed the ``lads`` extra.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, suppress
from typing import Any

from ofplang.run.simulator import DeviceComputationError

from labcode import connections
from labcode.connections import NO_CONNECTION, NOT_REQUESTED, OTHER_PROTOCOL, failing_code
from labcode.extension import (
    CONNECTION_KIND_LADS,
    LADS_CLIENT_LOCAL,
    LADS_CLIENTS_LOCAL,
    TLS_UNSUPPORTED,
    Connection,
)
from labcode.lads_unit import LadsUnit, open_unit

__all__ = [
    "LADS",
    "NOT_REQUESTED",
    "NO_CONNECTION",
    "OTHER_PROTOCOL",
    "Target",
    "connect",
    "endpoint_url",
    "failing_code",
    "plan_clients",
    "session",
    "wrap",
]

#: How the shared machinery (`labcode.connections`) names this protocol in messages and codes.
LADS = connections.Protocol(label="LADS", prefix="lads")

#: How often, in seconds, asyncua's supervisor probes a connection it holds (see `connect`).
WATCHDOG_INTERVAL = 30.0

#: One machine to connect to: ``(id, host, port, insecure, device, unit)``.
Target = tuple[str, str, int, bool, str | None, str | None]


def endpoint_url(host: str, port: int) -> str:
    """The OPC UA endpoint a connection's host and port name."""
    return f"opc.tcp://{host}:{port}/"


def connect(
    host: str,
    port: int,
    *,
    insecure: bool = False,
    device: str | None = None,
    unit: str | None = None,
) -> LadsUnit:
    """Connect to the LADS server at ``host:port`` and return the functional unit to drive.

    Raises `DeviceComputationError` -- a graceful operation failure -- when the client library
    is missing, the connection would need security (refused as SiLA2 refuses TLS, §1.5), or the
    server does not have the unit asked for (or has several and none was named)."""
    if not insecure:
        raise DeviceComputationError(
            f"cannot connect to {host}:{port}: {TLS_UNSUPPORTED}", code="lads_tls_unsupported"
        )
    try:
        from asyncua import ua  # noqa: PLC0415 - deliberately a local import
        from asyncua.sync import Client, ThreadLoop  # noqa: PLC0415
    except ImportError as exc:
        raise DeviceComputationError(
            "the LADS OPC UA client library (asyncua) is not importable by the interpreter "
            "running this script; install it there (`uv sync --extra lads`, or "
            "`pip install 'labcode[lads]'`)",
            code="lads_unavailable",
        ) from exc
    # asyncua's synchronous client runs its event loop on a thread, and by default that thread
    # is not a daemon: a client left open -- a raw script that never closes it, or one that
    # raises before it can -- would then keep the child process alive until `op_timeout` killed
    # it, and so would a connection that failed half way (asyncua does not stop the thread when
    # `connect` raises). So the loop is ours: a daemon, stopped by `disconnect` as asyncua's own
    # would be (`close_tloop`), and stopped here if connecting fails.
    loop = ThreadLoop()
    loop.daemon = True
    loop.start()
    # asyncua's connection supervisor probes the server every `watchdog_intervall` seconds and,
    # with the same figure as the probe's timeout, declares the connection lost when an answer
    # is late -- after which every request fails with "client is disconnected". An answer is
    # late whenever the client's own loop is busy, and `load_data_type_definitions` below keeps
    # it busy generating classes for a second or more; on a loaded machine (the runner replans
    # in the parent while an operation connects) the default 1 s probe then fails a healthy
    # connection. The probe is not what notices a dead server here -- every wait polls the
    # server at least once a second, each request with its own timeout -- so it is made rare.
    client = Client(endpoint_url(host, port), tloop=loop, watchdog_intervall=WATCHDOG_INTERVAL)
    client.close_tloop = True
    try:
        client.connect()
    except BaseException:
        loop.stop()
        raise
    try:
        # Generates the LADS structures (KeyValueType, ...) that StartProgram's arguments and a
        # result's properties are encoded as.
        client.load_data_type_definitions()
        return open_unit(client, ua, device=device, unit=unit)
    except BaseException:
        with suppress(Exception):
            client.disconnect()
        raise


def session(
    targets: Sequence[Target], *, unavailable: Mapping[str, str] | None = None
) -> AbstractContextManager[tuple[dict[str, Any], Any]]:
    """Hold a unit open for each of `targets` for the duration of one operation; yields
    ``(clients, client)`` as `labcode.sila2.session` does.

    `connect` is looked up at call time, through this module, so replacing
    ``labcode.lads.connect`` replaces what a session opens with."""
    return connections.open_clients(
        targets,
        protocol=LADS,
        connect=lambda host, port, insecure, device, unit: connect(
            host, port, insecure=insecure, device=device, unit=unit
        ),
        close=lambda client: client.close(),
        unavailable=unavailable,
    )


def wrap(
    code: str,
    targets: Sequence[tuple[str, Connection]],
    *,
    unavailable: Mapping[str, str] | None = None,
) -> str:
    """`code` (a `flavor: lads` script body) with its connections opened around it, binding
    `LADS_CLIENTS_LOCAL` and `LADS_CLIENT_LOCAL`. The waits a script may want
    (`labcode.lads_commands`) arrive through the units themselves or an ordinary import."""
    literals = [
        f"({identifier!r}, {connection.host!r}, {connection.port}, {connection.insecure!r}, "
        f"{connection.device!r}, {connection.unit!r})"
        for identifier, connection in targets
    ]
    return connections.render_wrapper(
        code,
        module="labcode.lads",
        clients_local=LADS_CLIENTS_LOCAL,
        client_local=LADS_CLIENT_LOCAL,
        literals=literals,
        unavailable=unavailable,
    )


def plan_clients(
    machines: Iterable[tuple[Any, Mapping[str, Connection] | None]],
) -> tuple[list[tuple[str, Connection]], dict[str, str]]:
    """Split the machines an operation holds into the ones to open a LADS client to and the ones
    there is no client for; a machine reached over SiLA2 is one of the latter (G40)."""
    return connections.plan_clients(machines, kind=CONNECTION_KIND_LADS)
