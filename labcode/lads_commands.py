"""The waits a LADS instrument script needs, written once (SPECIFICATIONS.md §1.10.1).

The LADS counterpart of `labcode.sila2_commands`. Starting a program is only half of driving an
instrument: ``StartProgram`` returns a run id as soon as the run has begun, and the work goes on
on the server, so a script that does not wait would report success while the instrument is still
busy. The same goes for the unit's ``Stop`` / ``Clear`` and a cover's ``Open`` / ``Close``, which a
LADS server may acknowledge at once and carry out afterwards, reporting progress through the
state machine.

It arrives by an **ordinary import** where a script wants the functions themselves::

    from labcode.lads_commands import run_program

though a script usually reaches them through the unit it was handed (``lads_client.run_program``,
``lads_client.stop()`` ...), which calls these. Nothing is injected, for the reason given in
§1.7.1: a name that appears out of nowhere is worth spending only on what a script cannot obtain
for itself.

**A timeout is not a cancel.** LADS does offer ``Abort``, but a wait that times out does not call
it: it fails *the operation* and leaves the instrument as it is, exactly as a SiLA2 ``settle``
must (G42). The same mistake therefore leaves the lab in the same state whichever protocol the
instrument speaks, and restoring it is the operator's job either way.

**Timeouts are real seconds**, unrelated to a mode's ``duration`` and not rescaled by
``--seconds-per-tick``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ofplang.run.simulator import DeviceComputationError

if TYPE_CHECKING:
    from labcode.lads_unit import LadsUnit

#: How long `run_program` waits for a run's result, in **real seconds** -- the same generous
#: bound as SiLA2's `settle`, for the same reason: to turn a hang into a diagnosable failure.
DEFAULT_TIMEOUT = 3600.0

#: How often `run_program` looks for the result, in **real seconds** (as `settle`'s poll).
DEFAULT_POLL = 1.0

#: How long a state transition (Stop, Clear, a cover moving) may take to settle. Shorter than a
#: program's bound, but still generous: these are single movements, and one that does not settle
#: in ten minutes has hung.
TRANSITION_TIMEOUT = 600.0

#: How often a transition's state is read. Transitions are short, so they are watched closely.
TRANSITION_POLL = 0.2

#: The states a LADS state machine passes through while a method's effect is being carried out.
#: A wait for a transition ends at the first state that is none of these.
TRANSIENT_STATES = frozenset({"Stopping", "Aborting", "Clearing", "Opening", "Closing", "Starting"})

#: The result property that says how a run ended, where a server records one. LADS does not
#: define it; a server that does not provide it is judged by the unit's state instead.
OUTCOME_KEY = "Outcome"
COMPLETED = "Completed"


def run_program(
    unit: LadsUnit,
    template_id: str,
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
    **properties: Any,
) -> dict[str, str]:
    """Start the program `template_id` on `unit` and wait for its result.

    Returns the result's ``Properties`` as a dict of strings: the run's inputs, whatever the
    program recorded (a warning, a measurement ...), and the server's outcome where it records
    one.

    Raises `DeviceComputationError` when the server refuses the start (``lads_call_failed``),
    when no result appears within `timeout` (``lads_program_timeout``), or when the run did not
    complete (``lads_program_failed``) -- the result says ``Outcome`` other than ``Completed``,
    or, where the server records no outcome, the unit has ended ``Aborted``."""
    label = f"{unit.name} {template_id}"
    run_id = unit.start_program(template_id, **properties)
    result = wait_for_result(unit, run_id, label=label, timeout=timeout, poll=poll)
    outcome = result.get(OUTCOME_KEY)
    failed = outcome != COMPLETED if outcome is not None else unit.state() == "Aborted"
    if failed:
        reason = unit.last_error()
        detail = f": {reason}" if reason else ""
        raise DeviceComputationError(
            f"{label} did not complete ({outcome or 'unit ' + unit.state()}){detail}",
            code="lads_program_failed",
        )
    return result


def wait_for_result(
    unit: LadsUnit,
    run_id: str,
    *,
    label: str,
    timeout: float | None = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
) -> dict[str, str]:
    """Wait until `unit`'s ResultSet holds the result of run `run_id`, and return its Properties.

    A result is recognised by its ``DeviceProgramRunId`` property or, failing that, by its
    browse name being the run id."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        # Checked before sleeping, so a run that is already over costs no wait at all.
        result = _find_result(unit, run_id)
        if result is not None:
            return result
        if deadline is not None and time.monotonic() > deadline:
            raise DeviceComputationError(
                f"{label} (run {run_id}) recorded no result within {timeout:.0f}s. The "
                f"instrument may still be running it -- nothing was aborted, so the lab is left "
                f"as the run left it and restoring it is the operator's job.",
                code="lads_program_timeout",
            )
        time.sleep(poll)


def _find_result(unit: LadsUnit, run_id: str) -> dict[str, str] | None:
    for node in unit.results():
        if not _is_result_of(node, run_id):
            continue
        properties = _child_value(node, "Properties") or []
        return {str(pair.Key): str(pair.Value) for pair in properties}
    return None


def _is_result_of(node: Any, run_id: str) -> bool:
    recorded = _child_value(node, "DeviceProgramRunId")
    if recorded is not None:
        return str(recorded) == run_id
    return str(node.read_browse_name().Name) == run_id


def _child_value(node: Any, name: str) -> Any:
    for child in node.get_children():
        if child.read_browse_name().Name == name:
            return child.read_value()
    return None


def wait_for_state(
    unit: LadsUnit,
    *,
    owner: str = "FunctionalUnitState",
    label: str,
    timeout: float | None = TRANSITION_TIMEOUT,
    poll: float = TRANSITION_POLL,
) -> str:
    """Wait until the state machine at `owner` has left every transient state, and return the
    state it settled in."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        state = unit.state(owner)
        if state not in TRANSIENT_STATES:
            return state
        if deadline is not None and time.monotonic() > deadline:
            raise DeviceComputationError(
                f"{label} was still {state!r} after {timeout:.0f}s",
                code="lads_transition_timeout",
            )
        time.sleep(poll)
