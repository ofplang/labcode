"""The polling loop an instrument script needs, written once (SPECIFICATIONS.md §1.6.1).

Issuing a command is only half of driving an instrument: a SiLA2 **observable** command
returns a command *instance* immediately and does the work on the server, so a script that
does not wait would report success while the instrument is still busy. Every such script needs
the same loop, and before this module every one of them carried its own copy.

It arrives by an **ordinary import**, written in the script::

    from labcode.sila2_commands import settle

Nothing is injected. Unlike `sila2_client`, which a script has no way to obtain for itself,
this is a function in a module the script can already reach -- so making it appear out of
nowhere would buy nothing and cost a reserved name. It is therefore equally available to a
``raw`` script and to a ``flavor: sila2`` one, and the dependency stays visible in the code
that has it.

**Nothing here is automatic.** `settle` is a function the script chooses to call, exactly
where its author decides the wait belongs.

**A timeout is not a cancel.** SiLA2 gives labcode no way to stop a command it started, so a
`settle` that times out fails *the operation* while the instrument carries on. Whatever state
that leaves the lab in -- a closed lid with a plate inside, say -- is the operator's to
restore, as it is for any other operation that failed part way.

**The timeout is real seconds.** It is unrelated to the mode's declared ``duration`` (an
estimate, for scheduling, in environment time) and is not rescaled by ``--seconds-per-tick``.
A schedule's estimate is not a deadline, and this is not an estimate.
"""

from __future__ import annotations

import time
from typing import Any

from ofplang.run.simulator import DeviceComputationError

#: How long `settle` waits before giving up, in **real seconds**.
#:
#: Generous on purpose. Its job is to turn a hang into a diagnosable failure, not to express
#: a policy about how long an instrument may take: nothing else in the stack bounds an
#: operation, so without it a silent instrument stops the whole run with no status document
#: and no observation written. An operation that genuinely runs for hours should say so with
#: its own ``timeout=``; `None` waits forever, which is then a decision on the record rather
#: than an accident.
DEFAULT_TIMEOUT = 3600.0

#: How often `settle` looks, in **real seconds**.
#:
#: labcode observes its own operations on a period of ``poll_interval * seconds_per_tick``,
#: roughly 20 s by default (`labcode.backend.DEFAULT_SECONDS_PER_TICK`). Looking much more
#: often than that buys no accuracy the run can use, and spends a SiLA2 round trip each time.
DEFAULT_POLL = 1.0


def settle(
    instance: Any,
    label: str,
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
) -> Any:
    """Wait for the observable command `instance` to finish, and return its responses.

    `label` names the command in the failure message; it is required because a timeout that
    cannot say *which* command hung is most of the way to useless.

    Returns whatever ``instance.get_responses()`` returns -- so a command with no response
    is called for its effect and its return value ignored, and one with responses reads as
    ``responses = settle(feature.GetTapeLeft(), "GetTapeLeft")``.

    Raises `DeviceComputationError` -- a graceful operation failure, as a refused connection
    is -- when `timeout` elapses, or when `instance` is not an observable command's instance
    at all.
    """
    # An *unobservable* command has already completed when its call returns, and what it
    # returns is a plain response with no `get_responses`. Passing one here is an easy
    # mistake to make (the two kinds of command are called identically) and would otherwise
    # surface as an AttributeError from inside the loop below, naming nothing useful.
    #
    # `get_responses` is the thing checked, rather than `done`, because it is a method:
    # looking it up costs nothing, whereas `done` is a property whose evaluation is a real
    # question to the server -- and asking it here would spend a poll before the loop.
    if not callable(getattr(instance, "get_responses", None)):
        raise DeviceComputationError(
            f"{label}: settle() takes the command instance an *observable* SiLA2 command "
            f"returns, but this object has no 'get_responses' "
            f"({type(instance).__name__}). An unobservable command has already finished "
            f"when its call returns -- there is nothing to settle.",
            code="sila2_not_observable",
        )

    deadline = None if timeout is None else time.monotonic() + timeout
    # Checked before sleeping, so a command that is already finished costs no wait at all.
    while not instance.done:
        if deadline is not None and time.monotonic() > deadline:
            raise DeviceComputationError(
                f"{label} did not finish within {timeout:.0f}s. The instrument is still "
                f"running it -- a SiLA2 command cannot be cancelled from here, so the lab "
                f"is left as the command left it and restoring it is the operator's job.",
                code="sila2_command_timeout",
            )
        time.sleep(poll)
    return instance.get_responses()
