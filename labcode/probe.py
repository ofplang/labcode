"""Availability probing: which machines the run can still reach.

A real lab has machines that stop answering -- switched off, disconnected, halted. The
runner already knows what to do about that: it asks the backend which machines are down
each time it replans and schedules against an environment with those taken out, so the
work routes around them (and comes back when they return). What was missing is the
answer. This module produces it, by checking the addresses the environment declares
(`x-labcode.connection`) according to the policies it declares (`x-labcode.probe`).

**What a probe is.** Opening a TCP connection, and nothing more. It needs no client
library (so labcode probes with or without the `sila2` extra installed) and it is quick
and bounded. What it establishes is *reachability, not readiness*: a machine whose port is
open but whose software is wedged reads as up here, and that case surfaces where it
belongs -- as the operation that tried to command it failing.

**When it happens.** In the parent process, inline, on the replan that asks for it -- so
the cost is a run-loop pause bounded by (unreachable machines x their timeout). A slow
round shows up as the virtual clock advancing further on that tick, not as an error. The
default policy probes each machine once, at the start of the run; an `interval` re-probes,
which is also what lets a machine that comes back be used again.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from labcode.extension import (
    EXTENSION_KEY,
    Connection,
    Probe,
    declared_probe,
    device_connections,
    merge_probe,
    transporter_connections,
)

#: How a machine is checked: ``prober(host, port, timeout) -> reachable``. Injectable so a
#: test can decide what is reachable without a network.
Prober = Callable[[str, int, float], bool]

#: Told about each machine whose reachability changed: ``on_change(id, reachable)``.
ChangeReporter = Callable[[str, bool], None]

#: Told once when a poll cycle outgrew its poll period:
#: ``on_cadence_slip(skipped_ticks, budget_seconds, spent_seconds)``. It lives here
#: because probing is the loop's most expensive optional step and so the likeliest cause,
#: but the cost it reports is the whole cycle's -- see `LabcodeBackend.advance`.
CadenceReporter = Callable[[int, float, float], None]


def tcp_reachable(host: str, port: int, timeout: float) -> bool:
    """Whether a TCP connection to `host:port` opens within `timeout` seconds.

    Any socket-level failure -- refused, unresolvable, timed out -- means unreachable:
    the question is whether this run can talk to the machine, not why it cannot."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class Target:
    """One machine to probe: its environment id, its policy, and where it lives."""

    identifier: str
    policy: Probe
    connection: Connection


def probe_targets(environment: dict) -> list[Target]:
    """The machines `environment` asks to be probed, devices before transporters.

    A machine is included when its effective policy (the root's defaults under its own,
    field by field) is enabled *and* it declares a usable connection. A machine with no
    connection is not probed -- there is nothing to probe -- which the dialect validator
    rejects up front when a policy enabled it anyway."""
    root = declared_probe(environment.get(EXTENSION_KEY))
    targets: list[Target] = []
    for key, connections in (
        ("devices", device_connections(environment)),
        ("transporters", transporter_connections(environment)),
    ):
        overrides = _machine_policies(environment, key)
        for identifier, connection in connections.items():
            policy = merge_probe(root, overrides.get(identifier, {}))
            if policy.enabled:
                targets.append(Target(identifier, policy, connection))
    return targets


def _machine_policies(environment: dict, key: str) -> dict[str, dict]:
    """The probe fields each ``devices[]`` / ``transporters[]`` entry declares itself."""
    found: dict[str, dict] = {}
    entries = environment.get(key)
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        if isinstance(identifier, str):
            found[identifier] = declared_probe(entry.get(EXTENSION_KEY))
    return found


class Availability:
    """What the run has last seen of each probed machine's reachability.

    `down` is the answer the backend hands the runner; calling it re-probes whatever its
    policy says is due and caches the rest, so the runner may ask on every tick. Nothing
    is probed at all when there are no targets."""

    def __init__(
        self,
        targets: Iterable[Target],
        *,
        prober: Prober = tcp_reachable,
        monotonic: Callable[[], float] = time.monotonic,
        on_change: ChangeReporter | None = None,
    ) -> None:
        self._targets = list(targets)
        self._prober = prober
        self._monotonic = monotonic
        self._on_change = on_change
        # What the last probe found, and when it ran -- per machine, so policies with
        # different intervals stay independent.
        self._reachable: dict[str, bool] = {}
        self._checked: dict[str, float] = {}

    def down(self) -> set[str]:
        """The machines currently unreachable, re-probing what is due first."""
        if not self._targets:
            return set()
        now = self._monotonic()
        # One probe per address per round: two machines may live on one server, and
        # connecting twice to say the same thing is pure delay. The first policy that
        # needs the address decides the timeout.
        this_round: dict[tuple[str, int], bool] = {}
        for target in self._targets:
            if not self._due(target, now):
                continue
            endpoint = (target.connection.host, target.connection.port)
            if endpoint not in this_round:
                this_round[endpoint] = self._prober(
                    target.connection.host, target.connection.port, target.policy.timeout
                )
            self._record(target.identifier, this_round[endpoint], now)
        return {identifier for identifier, ok in self._reachable.items() if not ok}

    def _due(self, target: Target, now: float) -> bool:
        """Whether `target` needs probing now: always the first time; then according to
        its interval -- never again for `once`, every round for 0, otherwise on age."""
        last = self._checked.get(target.identifier)
        if last is None:
            return True
        interval = target.policy.interval
        if interval is None:
            return False
        return now - last >= interval

    def _record(self, identifier: str, reachable: bool, now: float) -> None:
        previous = self._reachable.get(identifier)
        self._reachable[identifier] = reachable
        self._checked[identifier] = now
        if self._on_change is None:
            return
        # A machine that starts out unreachable is news; one that starts out reachable is
        # what was expected, so only a later change is worth saying.
        if (previous is None and not reachable) or (previous is not None and previous != reachable):
            self._on_change(identifier, reachable)


def build_availability(
    environment: dict,
    *,
    prober: Prober | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    on_change: ChangeReporter | None = None,
) -> Availability | None:
    """An `Availability` for `environment`, or None when it asks for no probing at all
    (so a run that does not use the feature carries none of its cost)."""
    targets = probe_targets(environment)
    if not targets:
        return None
    return Availability(
        targets, prober=prober or tcp_reachable, monotonic=monotonic, on_change=on_change
    )
