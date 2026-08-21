"""The one seam between labcode's execution path and whatever records what it did.

OpenTelemetry is how labcode records a run today; it is **not a settled decision**. So the
execution path -- `labcode.backend`, `labcode.runner`, `labcode.run_cli`, `labcode._child` --
imports this module and nothing else about recording, and never sees a type belonging to a
recording backend. What crosses this seam is labcode's own vocabulary: a run began, an operation
began, an operation finished, and the environment a child process should inherit.

**Swapping the recording backend.** Implement `Recorder` (and
`labcode.sila2_instrument.Sink`, for what a script does to an instrument), then add one line to
`build_recorder` and one to `_resume`. The files to touch are:

* this module -- the two factories below, one line each;
* a new module beside `labcode.otel` / `labcode.otel_sila2` holding the implementations.

Nothing else. `labcode.sila2_instrument` -- where the difficult part lives (which calls to
intervene in, how an observable command's real extent is observed) -- is reused unchanged, and
the execution path does not move at all. A test asserts that last part rather than trusting it:
if a module in the execution path ever names OpenTelemetry directly, it fails.

**Recording never breaks a run.** That guarantee lives here, once: `build_recorder` wraps its
implementation so that every call across this seam swallows its own failures. An implementation
is therefore written plainly, and a future one inherits the guarantee without knowing about it.
A run that cannot be recorded is still a run; a run stopped by its own bookkeeping is not.
"""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol

from labcode.sila2_instrument import close_open

# -- the vocabulary of the record --------------------------------------------------------

#: One `lc run`: the whole record of one execution of one workflow.
SPAN_RUN = "run"
#: One dispatched operation. Names are aggregation keys, so the process is in the name and
#: everything that varies per operation (the node, the Object id) is an attribute.
SPAN_PROCESS_PREFIX = "process "
SPAN_TRANSPORT = "transport"

#: The campaign this run belongs to. labcode gives it no meaning -- it is recorded and nothing
#: else -- and it is left out entirely when nobody supplied one.
ATTR_MISSION = "mission.id"

#: Where in the workflow this operation came from, and what it was assigned to. These are
#: *pointers* into the workflow and environment documents, not copies of them.
ATTR_NODE = "ofp.node"
ATTR_PROCESS = "ofp.process"
ATTR_MODE = "ofp.mode"
ATTR_SPOT_FROM = "ofp.spot.from"
ATTR_SPOT_TO = "ofp.spot.to"
ATTR_TRANSPORTER = "ofp.transporter"
#: Which physical Objects this operation handled (labcode's implicit ``_id``) -- the attribute
#: that answers "what happened to this plate". **Always a list**, because one operation may hold
#: several Object ports and may create an Object as well as consume one, and losing one of them
#: is the kind of gap nobody notices later. Not an identity of the operation: that is the record's
#: own span id, and `ATTR_NODE` says which node it came from.
ATTR_OBJECT_ID = "ofp.object.id"
#: The plan's interval for this operation, in environment time. Recorded beside the real
#: timestamps rather than instead of them: the record is of real time, and how far it drifted
#: from the plan is the interesting part.
ATTR_TICK_START = "ofp.tick.start"
ATTR_TICK_END = "ofp.tick.end"

#: An operation still running when the run ended. Not a failure of the operation: nobody
#: observed how it finished, which is a different thing from finishing badly.
RUN_STOPPED = "run_stopped"

#: An operation the backend reports as failed without saying why (an injected fault). A real
#: failure carries its own reason code, which is recorded instead.
OP_FAILED = "failed"


class Recorder(Protocol):
    """What the execution path tells the record. One implementation per recording backend.

    Operations are keyed by the backend's operation id, because that is what the execution path
    has: it dispatches an operation, learns much later that it finished, and never holds
    anything belonging to the record in between.
    """

    def run_started(self, *, mission_id: str | None = None) -> str | None:
        """Begin the run's record; return an id an operator can find it by, if there is one."""
        ...

    def run_finished(self, *, error_type: str | None = None, message: str | None = None) -> None:
        """End the run's record."""
        ...

    def op_started(self, uuid: str, name: str, attributes: Mapping[str, Any]) -> None:
        """Begin the record of operation `uuid`."""
        ...

    def op_active(self, uuid: str) -> AbstractContextManager[None]:
        """A stretch in which operation `uuid` is the current one.

        Wrapped around dispatch, so that what dispatch starts -- a child process -- can be tied
        to the operation that started it."""
        ...

    def op_finished(
        self,
        uuid: str,
        *,
        error_type: str | None = None,
        message: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """End the record of operation `uuid`.

        `attributes` are what only completion could tell -- the identity of an Object the
        operation *created*, which is minted when it finishes. They are added to what was
        recorded at dispatch, so a superset replaces the value set then."""
        ...

    def child_env(self) -> Mapping[str, str] | None:
        """Environment variables a child process needs in order to record what it does, and to
        have that land under the current operation.

        Opaque to the caller: it passes them to the child and asks no questions -- which is why
        an implementation may put anything its own recording needs in here, not only what
        identifies the operation. `None` when this child is to record nothing."""
        ...

    def shutdown(self) -> None:
        """Finish recording (a backend that buffers flushes here)."""
        ...


class NullRecorder:
    """Records nothing. What a run gets unless it was asked to record."""

    def run_started(self, *, mission_id: str | None = None) -> str | None:
        return None

    def run_finished(self, *, error_type: str | None = None, message: str | None = None) -> None:
        return None

    def op_started(self, uuid: str, name: str, attributes: Mapping[str, Any]) -> None:
        return None

    def op_active(self, uuid: str) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def op_finished(
        self,
        uuid: str,
        *,
        error_type: str | None = None,
        message: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        return None

    def child_env(self) -> Mapping[str, str] | None:
        return None

    def shutdown(self) -> None:
        return None


class _Guarded:
    """A `Recorder` whose failures stop at this seam (see the module docstring)."""

    def __init__(self, inner: Recorder) -> None:
        self._inner = inner

    def run_started(self, *, mission_id: str | None = None) -> str | None:
        try:
            return self._inner.run_started(mission_id=mission_id)
        except Exception:
            return None

    def run_finished(self, *, error_type: str | None = None, message: str | None = None) -> None:
        with contextlib.suppress(Exception):
            self._inner.run_finished(error_type=error_type, message=message)

    def op_started(self, uuid: str, name: str, attributes: Mapping[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self._inner.op_started(uuid, name, attributes)

    def op_active(self, uuid: str) -> AbstractContextManager[None]:
        try:
            return _quietly(self._inner.op_active(uuid))
        except Exception:
            return contextlib.nullcontext()

    def op_finished(
        self,
        uuid: str,
        *,
        error_type: str | None = None,
        message: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        with contextlib.suppress(Exception):
            self._inner.op_finished(
                uuid, error_type=error_type, message=message, attributes=attributes
            )

    def child_env(self) -> Mapping[str, str] | None:
        try:
            return self._inner.child_env()
        except Exception:
            return None

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._inner.shutdown()


@contextlib.contextmanager
def _quietly(inner: AbstractContextManager[Any]) -> Iterator[None]:
    """`inner`, entered and left without its failures reaching the body.

    Deliberately not shared with the near-identical helper in `labcode.sila2_instrument`: that
    module is meant to be copied to a program that drives instruments directly, and it can only
    be copied on its own if it depends on nothing here."""
    entered = False
    with contextlib.suppress(Exception):
        inner.__enter__()
        entered = True
    try:
        yield
    finally:
        if entered:
            with contextlib.suppress(Exception):
                inner.__exit__(None, None, None)


# -- the two factories (the only places that know an implementation exists) ---------------


def build_recorder(*, enabled: bool) -> Recorder:
    """The `Recorder` for this run: a real one when `enabled`, otherwise one that records nothing.

    Recording is off by default (`lc run --trace` turns it on), so the usual answer here is
    `NullRecorder` -- which is also why the execution path calls this seam unconditionally
    instead of asking whether recording is on.

    A run that asked to record and cannot -- the extra is not installed -- gets a warning and
    runs unrecorded, rather than failing."""
    if not enabled:
        return NullRecorder()
    try:
        from labcode.otel import OtelRecorder

        return _Guarded(OtelRecorder())
    except Exception as exc:
        warnings.warn(
            f"labcode: cannot record this run (install the extra: `pip install "
            f"'labcode[otel]'`): {exc}",
            stacklevel=2,
        )
        return NullRecorder()


@contextlib.contextmanager
def child_recording() -> Iterator[None]:
    """Record what this child process does, if its parent asked it to.

    The child is told through its environment, and what to look for there is the
    implementation's business -- so this asks it to resume rather than deciding itself.
    Nothing in the environment means the parent is not recording, which is the common case and
    not a problem.

    On the way out, commands whose responses were never collected are closed first (they are
    part of what this process did) and only then is the record finished, so nothing is dropped
    before it is written."""
    session = _resume()
    if session is None:
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            close_open()
        with contextlib.suppress(Exception):
            session.finish()


def _resume() -> Any:
    """The implementation's child-side session, or `None` if this process records nothing.

    A failure here is silent on purpose: this runs in a child whose output belongs to the user's
    script, and the parent has already said its piece if recording was impossible."""
    try:
        from labcode.otel import resume_from_env

        return resume_from_env()
    except Exception:
        return None
