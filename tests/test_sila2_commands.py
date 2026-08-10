"""Tests for `labcode.sila2_commands` -- the helper surface a `sila2` script is handed.

`settle` is a polling loop, so the interesting cases are about *what it polls* and *when it
gives up*: none of them need a network, and none of them should need real time to pass. The
fake command below finishes after a set number of looks, and `time` is replaced so a timeout
can be reached without waiting for it.
"""

from __future__ import annotations

import pytest
from ofplang.run.simulator import DeviceComputationError

from labcode import sila2_commands


class _FakeCommand:
    """An observable command's instance: `done` turns True after `finishes_after` reads."""

    def __init__(self, finishes_after: int = 0, responses: object = "responses"):
        self._left = finishes_after
        self._responses = responses
        self.looks = 0

    @property
    def done(self) -> bool:
        self.looks += 1
        if self._left <= 0:
            return True
        self._left -= 1
        return False

    def get_responses(self) -> object:
        return self._responses


class _FakeResponse:
    """What an *unobservable* command returns: a plain response, with no `done`."""

    InstrumentWarningMessage = ""


@pytest.fixture
def no_waiting(monkeypatch):
    """Make `sleep` advance a fake clock instead of blocking, and hand back the log of
    sleeps -- so a test can assert on the polling cadence without spending it."""
    clock = {"now": 0.0}
    slept: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(sila2_commands.time, "monotonic", monotonic)
    monkeypatch.setattr(sila2_commands.time, "sleep", sleep)
    return slept


def test_a_finished_command_returns_its_responses_without_waiting(no_waiting):
    # Checked before sleeping, so the common case -- a command that is already done -- costs
    # no wait at all.
    instance = _FakeCommand(finishes_after=0, responses={"cycles": 7})

    assert sila2_commands.settle(instance, "StartCycle") == {"cycles": 7}
    assert no_waiting == []


def test_it_polls_until_the_command_finishes(no_waiting):
    instance = _FakeCommand(finishes_after=3)

    assert sila2_commands.settle(instance, "StartCycle") == "responses"
    # Three sleeps for three unfinished looks, at the default cadence.
    assert no_waiting == [sila2_commands.DEFAULT_POLL] * 3


def test_the_poll_interval_is_the_caller_s(no_waiting):
    sila2_commands.settle(_FakeCommand(finishes_after=2), "StartCycle", poll=5.0)

    assert no_waiting == [5.0, 5.0]


def test_a_command_that_never_finishes_times_out(no_waiting):
    # `finishes_after` beyond any number of polls the timeout allows: the loop has to give
    # up on its own rather than run forever.
    instance = _FakeCommand(finishes_after=10_000)

    with pytest.raises(DeviceComputationError) as raised:
        sila2_commands.settle(instance, "StartRun", timeout=10.0, poll=1.0)

    assert raised.value.code == "sila2_command_timeout"
    # The message has to name the command, and say that the instrument is still going --
    # a timeout here fails the operation but cancels nothing.
    assert "StartRun" in str(raised.value)
    assert "still" in str(raised.value)


def test_timeout_none_waits_indefinitely(no_waiting):
    # `None` is the way to say "wait as long as it takes" deliberately; a command that does
    # finish, however late, still finishes.
    instance = _FakeCommand(finishes_after=5_000)

    assert sila2_commands.settle(instance, "StartRun", timeout=None) == "responses"
    assert len(no_waiting) == 5_000


def test_an_unobservable_command_s_response_is_rejected(no_waiting):
    # `SetSealingTime` and friends have already completed when the call returns, and what
    # they return has no `get_responses`. Settling one is a mistake worth naming, rather
    # than an AttributeError from inside the loop.
    with pytest.raises(DeviceComputationError) as raised:
        sila2_commands.settle(_FakeResponse(), "SetSealingTime")

    assert raised.value.code == "sila2_not_observable"
    assert "SetSealingTime" in str(raised.value)
    assert "observable" in str(raised.value)


def test_the_guard_does_not_spend_a_poll(no_waiting):
    # `done` is a property, and on a real client evaluating it is a question to the server.
    # The guard must not ask it: a command that is already finished has to cost exactly one
    # look, not two.
    instance = _FakeCommand(finishes_after=0)

    sila2_commands.settle(instance, "StartCycle")

    assert instance.looks == 1


def test_the_default_timeout_is_generous_and_finite():
    # Finite on purpose: nothing else in the stack bounds an operation, so an infinite
    # default would let one silent instrument stop a run with nothing written down. Generous
    # on purpose: its job is to catch a hang, not to police how long instruments may take.
    assert sila2_commands.DEFAULT_TIMEOUT == 3600.0
    # And no finer than the runner's own observation cadence deserves.
    assert sila2_commands.DEFAULT_POLL >= 1.0
