"""What a connecting script flavor needs whatever protocol it speaks.

A `flavor: sila2` or `flavor: lads` script (SPECIFICATIONS.md §1.7, §1.10) is the *commands
alone*: labcode opens a client to each machine the operation holds, runs the code with them in
scope, and closes them afterwards. Most of that is the same for both protocols, and is here:

- deciding which of the operation's machines to open a client to and which to report as held
  without one (`plan_clients`);
- the mapping a script finds its clients in, which tells a typo apart from a machine that has
  no client (`Clients`), and the falsy stand-in that explains the latter (`Unconnected`);
- holding every client open for one operation and closing each on any exit (`open_clients`);
- generating the few lines that put the clients in the script's scope (`render_wrapper`), and
  the failing body used when there is nothing to open (`failing_code`).

What differs is only how one client is opened and what the protocol is called in messages and
error codes -- a `Protocol`. `labcode.sila2` and `labcode.lads` each bind these to their own.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from ofplang.run.simulator import DeviceComputationError

from labcode.extension import Connection

#: Why an operation holds a machine but opens no client to it. The key is what a wrapper writes
#: into the generated code; `Protocol.reason` turns it into the ``(error code, explanation)`` a
#: script gets if it uses the machine anyway. Keys are kept short because they travel as literals.
NO_CONNECTION = "no_connection"
NOT_REQUESTED = "not_requested"
#: The machine has an address, but for the other protocol: a script speaks one (§1.5).
OTHER_PROTOCOL = "other_protocol"


@dataclass(frozen=True)
class Protocol:
    """How a protocol is named where the shared machinery speaks: `label` in messages
    ("SiLA2"), `prefix` in error codes and the short session message ("sila2")."""

    label: str
    prefix: str

    def reason(self, reason: str) -> tuple[str, str]:
        """The ``(error code, explanation)`` for a machine held without a client. A reason this
        version does not know is reported as-is rather than lost."""
        known = {
            NO_CONNECTION: (f"{self.prefix}_not_connected", "it declares no x-labcode.connection"),
            NOT_REQUESTED: (
                f"{self.prefix}_endpoints_not_requested",
                "this transport does not ask for the clients of the devices at either end of its "
                "route; add `endpoints: true` to its x-labcode.script",
            ),
            OTHER_PROTOCOL: (
                f"{self.prefix}_other_protocol",
                f"its x-labcode.connection is not a {self.label} one, and a script speaks one "
                "protocol",
            ),
        }
        return known.get(reason, (f"{self.prefix}_not_connected", reason))


class Unconnected:
    """Stands in for a machine the operation holds but has no client for.

    Two things it must do, neither of which a `KeyError` on an id can. It is **falsy**, so
    ``if clients[some_id]:`` reads as "is there a client for it" -- a script that can work
    either way needs no knowledge of how the absence is represented. And using it anyway
    **fails the operation with why there is no client**, which is a fact about the environment
    (a device with no address) that no amount of reading the script reveals."""

    def __init__(self, identifier: str, reason: str, protocol: Protocol) -> None:
        self._identifier = identifier
        self._reason = reason
        self._protocol = protocol

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        _code, explanation = self._protocol.reason(self._reason)
        return f"<no {self._protocol.label} client for {self._identifier!r}: {explanation}>"

    def __getattr__(self, name: str) -> Any:
        # A dunder lookup comes from generic machinery (copy, pickle, a test runner's
        # introspection), never from a script commanding an instrument, and such machinery
        # relies on `AttributeError` to fall back. Only a real attempt to drive the machine is
        # worth failing the operation over.
        if name.startswith("__"):
            raise AttributeError(name)
        code, explanation = self._protocol.reason(self._reason)
        raise DeviceComputationError(
            f"labcode: no {self._protocol.label} client for {self._identifier!r}: {explanation}",
            code=code,
        )


class Clients(dict):
    """The clients an operation opened, by machine id, in the order they were opened.

    An ordinary mapping -- ``in``, ``.get()``, iteration and equality are the dict's -- with one
    addition: indexing a machine the operation *holds* without a client yields an `Unconnected`
    explaining itself, while indexing an id the operation does not hold at all raises, naming
    what it does hold. The distinction is the point: the first is a fact about the environment a
    script may legitimately have to handle, the second is a typo, and turning a typo into a
    falsy object would let it survive until something odd happened later."""

    def __init__(self, unavailable: Mapping[str, str], protocol: Protocol) -> None:
        super().__init__()
        self._unavailable = dict(unavailable)
        self._protocol = protocol

    def __missing__(self, key: Any) -> Any:
        reason = self._unavailable.get(key) if isinstance(key, str) else None
        if reason is None:
            raise KeyError(
                f"labcode: {key!r} is not a machine this operation holds "
                f"(it holds {sorted([*self, *self._unavailable])})"
            )
        return Unconnected(key, reason, self._protocol)


@contextmanager
def open_clients(
    targets: Sequence[tuple[Any, ...]],
    *,
    protocol: Protocol,
    connect: Callable[..., Any],
    close: Callable[[Any], None],
    unavailable: Mapping[str, str] | None = None,
) -> Iterator[tuple[dict[str, Any], Any]]:
    """Hold a client open to each of `targets` for the duration of one operation.

    Each target is ``(id, host, port, ...)``; ``connect(host, port, ...)`` opens its client.
    Yields ``(clients, client)``: the clients by id in `targets` order, and the first of them
    (the alias a single-machine operation uses). Every client that was opened is closed on the
    way out -- whether the body returned, the body raised, or a *later* connection failed --
    because each one's close is registered the moment it opens.

    A failure to connect is a graceful operation failure naming the machine, so a lab that is
    not reachable reads as "this op could not talk to that instrument" rather than as a library
    traceback."""
    if not targets:  # the validator rejects this; a direct caller might still do it
        raise DeviceComputationError(
            f"a {protocol.prefix} session needs at least one connection to open",
            code=f"{protocol.prefix}_no_target",
        )
    clients: dict[str, Any] = Clients(unavailable or {}, protocol)
    with ExitStack() as stack:
        for identifier, host, port, *rest in targets:
            try:
                client = connect(host, port, *rest)
            except Exception as exc:
                code = getattr(exc, "code", None) or f"{protocol.prefix}_connect_failed"
                raise DeviceComputationError(
                    f"cannot reach {identifier!r} at {host}:{port}: {exc}", code=code
                ) from exc
            # Registered before the next connection is attempted, so a failure there still
            # closes this one.
            stack.callback(_quietly, close, client)
            clients[identifier] = client
        yield clients, next(iter(clients.values()))


def _quietly(close: Callable[[Any], None], client: Any) -> None:
    """Close `client`, ignoring a failure to do so: an operation's outcome is what the script
    computed, and a channel that would not shut down cleanly must not replace it."""
    with suppress(Exception):
        close(client)


# -- generating the wrapper ------------------------------------------------------------------


def render_wrapper(
    code: str,
    *,
    module: str,
    clients_local: str,
    client_local: str,
    literals: Sequence[str],
    unavailable: Mapping[str, str] | None = None,
) -> str:
    """`code` with its connections opened around it: ``with <module>.session([...]) as
    (<clients_local>, <client_local>):`` followed by the body, indented.

    `literals` are the targets, already written as Python tuple literals. `unavailable` is
    written into the call when there is any and left out entirely when there is not, so an
    operation whose every machine is reachable generates exactly what it always did. An empty
    body falls back to ``pass`` so the ``with`` still compiles."""
    targets = ",\n".join(f"    {literal}" for literal in literals)
    held = ""
    if unavailable:
        pairs = ", ".join(
            f"{identifier!r}: {reason!r}" for identifier, reason in unavailable.items()
        )
        held = f", unavailable={{{pairs}}}"
    body = textwrap.indent(code, "    ").rstrip() or "    pass"
    return (
        f"from {module} import session as __lc_session\n"
        f"with __lc_session([\n{targets},\n]{held}) as ({clients_local}, {client_local}):\n"
        f"{body}\n"
    )


def failing_code(reason: str) -> str:
    """A script body that fails its operation with `reason`.

    Used where a connecting script cannot be run at all (no connection to open). It is returned
    as *code* rather than raised, because a code resolver runs inside dispatch, where an
    exception would escape the run rather than fail one operation."""
    return f"raise RuntimeError({reason!r})\n"


def plan_clients(
    machines: Iterable[tuple[Any, Mapping[str, Connection] | None]],
    *,
    kind: str | None = None,
) -> tuple[list[tuple[str, Connection]], dict[str, str]]:
    """Split the machines an operation holds into the ones to open a client to and the ones
    there is no client for.

    `machines` is ``(id, where to look that id up)`` in the order the operation holds them -- a
    mode's ``devices[]``, or a transport's transporter followed by the devices at either end of
    its route. Each id is looked up in **its own** map, so a device and a transporter that
    happen to share an id cannot shadow each other, and the first mention of an id wins, so a
    machine held twice is connected to once. A map of ``None`` says the operation holds that
    machine but is **not asking to drive it** (a transport without `endpoints`), which is a
    different fact from having no address for it, and is reported as one. With `kind`, a
    connection for another protocol is reported as `OTHER_PROTOCOL` rather than opened.

    Returns ``(targets, unavailable)``: what to connect, in that order, and ``{id: reason}`` for
    the rest. Holding a machine there is no client for is **not** an error -- an operation may
    occupy a device it does not drive -- so this is a filter, not a requirement; what an empty
    `targets` means is the caller's to decide."""
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
        elif kind is not None and connection.kind != kind:
            unavailable[identifier] = OTHER_PROTOCOL
        else:
            targets.append((identifier, connection))
    return targets, unavailable
