"""Measuring what an operation actually did to an instrument -- the backend-independent half.

What a labcode operation does to a machine does not appear in labcode's own code: it is what a
script writes against a `sila2` client. So this module intervenes in the `sila2` classes
themselves, and takes **one connection and one command** as the units worth measuring. A new
Feature or Command therefore becomes measurable by existing, with no telemetry code added to any
script.

**Not everything a client does is worth a unit.** Building one makes `sila2` fetch the definition
of every feature the server implements -- nine RPCs for one machine in the reference lab, and over
half of everything a real run recorded. Those are not measured (`_building_a_client`), because
they are the same RPCs a gRPC instrumentation records, and it records them *under* the connection;
measuring them here as well would count one call twice. What they cost stays visible as most of
`sila2.connect`, which is a unit.

**It does not know where the record goes.** Turning a measured unit into a record is the `Sink`'s
job (`labcode.otel_sila2` is the OpenTelemetry one); what lives here is only the *semantics* --
which calls are worth measuring, when one begins, and when it has really ended. Replacing the
recording backend leaves that semantics untouched, which is the point of the split: the semantics
is the hard-won part, the mapping is mechanical. Nothing here configures a provider either (that
is the application's job).

**The intervention is on class attributes.** `sila2.client.utils.call_rpc_function` looks like the
one choke point every unary RPC passes through, but each module holds its own reference to it
(`from ... import`), so replacing that single symbol would not be seen. Hence four methods:

* `SilaClient.__init__` -- the connection (it fetches every feature definition, so it is a real
  cost this dialect pays once per operation).
* `ClientUnobservableCommand.__call__` -- request and response in one call.
* `ClientObservableCommand.__call__` -- the **start** of an execution.
* `ClientObservableCommandInstance.get_responses` -- the **end** of one.

**Only the window where an RPC runs is made active.** An observable command's measured span runs
from its start to the collection of its responses, and *arbitrary script code sits in between* --
making that whole stretch the current context would swallow unrelated measurements as children of
this one. The RPCs, however, run inside these wrappers only, so those windows alone are wrapped in
`Sink.active`. That is what lets the gRPC instrumentation land its RPCs under the right command,
and every feature-definition fetch under the connection (`labcode.otel_sila2.instrument_grpc`).

**`get_responses()` is not the signal that a command finished.** It may be called more than once,
and it raises `CommandExecutionNotFinished` while the instrument is still working. Closing the
measurement whenever it is called would cut short a command that is still running -- so a
measurement is closed only when the collection *succeeded*, or failed with something other than
"not finished", and it is closed exactly once. A command whose responses are never collected is
closed as a failure by `close_open`.

**Measuring must not break the operation.** A `Sink` that raises, an object without the attribute
we wanted, an intervention that cannot be applied at all: each degrades to "not recorded". The one
thing that must not happen quietly is the *intervention* failing -- a run asked to record and
recording nothing is worth a warning, because nothing else would reveal it.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import threading
import warnings
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

# -- the vocabulary of the record --------------------------------------------------------

#: The connection's measured unit.
SPAN_CONNECT = "sila2.connect"

#: A command's unit is ``sila2 <Feature>.<Command>``. It is an aggregation key, so nothing
#: high-cardinality (an address, an Object id) goes in the name -- those are attributes.
SPAN_COMMAND_PREFIX = "sila2 "

#: "A call out to another service", for a `Sink` to map onto its own vocabulary.
KIND_CLIENT = "client"

ATTR_ADDRESS = "server.address"
ATTR_PORT = "server.port"
ATTR_FEATURE = "sila.feature"
ATTR_COMMAND = "sila.command"
ATTR_FQI = "sila.fully_qualified_identifier"
ATTR_EXECUTION_UUID = "sila.execution_uuid"

#: The failure recorded for a command whose responses the script never collected. It exists so
#: that an abandoned wait cannot be read as a command that legitimately took that long.
UNSETTLED = "sila2_command_unsettled"
UNSETTLED_MESSAGE = "script ended without collecting the command responses"

#: What a process must have in its **environment before it starts** if it is going to drive a
#: SiLA2 instrument *and* import anything else that uses protobuf.
#:
#: `sila2` needs protobuf's pure-Python implementation: it ships a generated
#: ``SiLAFramework_pb2`` and generates another at runtime for each server's features, and the C
#: implementation refuses the second registration ("duplicate file name SiLAFramework.proto").
#: It asks for that by setting this variable while *it* is imported -- which protobuf only reads
#: the first time protobuf itself is imported, so it works right up until something imports
#: protobuf first. Passing the same thing in the environment asks for it without depending on
#: import order at all, which is the only robust answer for a process that also builds, say, an
#: exporter that speaks protobuf.
#:
#: Set unconditionally rather than only when it is unset, because `sila2` overrides it too. A
#: process where it is not ``python`` cannot open a client at all, so honouring a different
#: value would only make a run fail in one mode and work in another.
PROTOBUF_ENV = {"PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python"}

#: Where a client's address is stashed. `SilaClient` keeps its own under a name-mangled
#: attribute, so it is read from the call instead and left here for a command to find.
_ENDPOINT_ATTR = "_labcode_endpoint"
#: The handle of the measurement a running observable command belongs to.
_HANDLE_ATTR = "_labcode_record_handle"


class Sink(Protocol):
    """Where measured units go: one implementation per recording backend.

    The **handle** `start` returns is opaque -- this module never looks inside it. `active` marks
    the stretch in which a unit is "the current one" (an implementation with no notion of a
    current unit may return an empty context).
    """

    def start(self, name: str, *, kind: str, attributes: Mapping[str, Any]) -> Any:
        """Begin a unit and return its handle."""
        ...

    def update(self, handle: Any, attributes: Mapping[str, Any]) -> None:
        """Add attributes learned after the unit began (an execution id, say)."""
        ...

    def end(
        self, handle: Any, *, error_type: str | None = None, message: str | None = None
    ) -> None:
        """Finish a unit; `error_type` records it as a failure."""
        ...

    def active(self, handle: Any) -> AbstractContextManager[None]:
        """A stretch in which `handle` is the current unit."""
        ...


@dataclass(frozen=True)
class Targets:
    """The classes to intervene in, and the exception meaning "still running".

    `resolve_targets` collects the real ones from `sila2`; a test passes stand-ins of the same
    shape, so the semantics can be verified in an interpreter that never installed the extra --
    the same seam as the injectable `spawn` / `prober` elsewhere in labcode.

    The four intervened classes are typed loosely on purpose: what is required of them is that
    they carry the method being replaced, which is a duck-typed fact about a class object rather
    than something a nominal type can state. `not_finished` is different -- it is used in an
    `isinstance` check, so it has to be an exception class.
    """

    client: Any
    unobservable: Any
    observable: Any
    instance: Any
    not_finished: type[BaseException] | None


@dataclass
class _State:
    sink: Sink
    targets: Targets
    #: The replaced ``(class, method name) -> original``, so `uninstrument_sila2` can put them back.
    originals: dict[tuple[type, str], Any] = field(default_factory=dict)
    #: Handles of commands that have not finished (what `close_open` closes).
    open_handles: list[Any] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


#: The instrumentation in force; `None` means nothing is recorded (a wrapper still installed then
#: simply passes through).
_state: _State | None = None


# -- applying and undoing ----------------------------------------------------------------


def resolve_targets() -> Targets:
    """Collect the real intervention points from `sila2`.

    Imported here rather than at module scope: `sila2` is an extra, and an interpreter that does
    not drive instruments is right not to have it."""
    from sila2.client.client_observable_command import ClientObservableCommand
    from sila2.client.client_observable_command_instance import ClientObservableCommandInstance
    from sila2.client.client_unobservable_command import ClientUnobservableCommand
    from sila2.client.sila_client import SilaClient

    not_finished: type[BaseException] | None
    try:
        from sila2.framework import CommandExecutionNotFinished

        not_finished = CommandExecutionNotFinished
    except ImportError:  # without it, an early collection cannot be told from a failure
        not_finished = None

    return Targets(
        client=SilaClient,
        unobservable=ClientUnobservableCommand,
        observable=ClientObservableCommand,
        instance=ClientObservableCommandInstance,
        not_finished=not_finished,
    )


def instrument_sila2(sink: Sink, *, targets: Targets | None = None) -> bool:
    """Have `sila2`'s calls measured into `sink`. Returns whether it could be applied.

    Idempotent: an existing instrumentation is undone first, so calling this twice measures each
    call once (with the later `sink`).

    When it cannot be applied it returns `False`, and **warns once if there was something to
    instrument**: `sila2` present but not shaped as expected (a class that is not there, a
    signature that moved) is a surprise nothing else in the output would reveal. `sila2` simply
    **not installed is not a surprise** and says nothing -- a script that drives no instruments is
    ordinary, and a script that does drive one fails loudly on its own when the library is
    missing. Either way the run continues: measuring is something a run carries, not what it is
    for."""
    uninstrument_sila2()
    try:
        resolved = targets if targets is not None else resolve_targets()
    except Exception as exc:
        if _sila2_available():
            _warn_not_applied(exc)
        return False

    state = _State(sink=sink, targets=resolved)
    try:
        _patch_client_init(state)
        _patch_unobservable_call(state)
        _patch_observable_call(state)
        _patch_get_responses(state)
    except Exception as exc:
        _restore(state)
        _warn_not_applied(exc)
        return False

    global _state
    _state = state
    return True


def uninstrument_sila2() -> None:
    """Put the intervened methods back (a no-op when nothing is instrumented)."""
    global _state
    state = _state
    if state is None:
        return
    _state = None
    _restore(state)


def close_open(*, error_type: str = UNSETTLED, message: str = UNSETTLED_MESSAGE) -> int:
    """Close every command that has not finished as a failure; return how many there were.

    What a script that never collected its responses leaves behind. Dropping those measurements
    open would leave calls with no end, which reads as though they never happened -- and closing
    them silently would make an abandoned wait look like a command that took that long."""
    state = _state
    if state is None:
        return 0
    with state.lock:
        handles = list(state.open_handles)
        state.open_handles.clear()
    for handle in handles:
        with contextlib.suppress(Exception):
            state.sink.end(handle, error_type=error_type, message=message)
    return len(handles)


def _sila2_available() -> bool:
    """Whether there is a SiLA2 library here at all -- which decides whether failing to
    instrument it is worth a word."""
    with contextlib.suppress(Exception):
        return importlib.util.find_spec("sila2") is not None
    return False


def _warn_not_applied(exc: BaseException) -> None:
    warnings.warn(
        f"labcode: cannot instrument SiLA2, so this run records no SiLA2 calls: {exc}",
        stacklevel=3,
    )


def _restore(state: _State) -> None:
    for (cls, name), original in state.originals.items():
        with contextlib.suppress(Exception):
            setattr(cls, name, original)
    state.originals.clear()


# -- talking to the sink (every failure degrades to "not recorded") -----------------------


def _start(name: str, attributes: Mapping[str, Any]) -> Any:
    state = _state
    if state is None:
        return None
    try:
        return state.sink.start(name, kind=KIND_CLIENT, attributes=attributes)
    except Exception:
        return None


def _update(handle: Any, attributes: Mapping[str, Any]) -> None:
    state = _state
    if state is None or handle is None:
        return
    with contextlib.suppress(Exception):
        state.sink.update(handle, attributes)


def _end(handle: Any, *, error_type: str | None = None, message: str | None = None) -> None:
    state = _state
    if state is None or handle is None:
        return
    _forget(handle)
    with contextlib.suppress(Exception):
        state.sink.end(handle, error_type=error_type, message=message)


@contextlib.contextmanager
def _active(handle: Any) -> Iterator[None]:
    """The window in which `handle` is the current unit.

    The call it wraps runs whether or not the `Sink` could give it a context."""
    state = _state
    entered: AbstractContextManager[None] | None = None
    if state is not None and handle is not None:
        try:
            entered = state.sink.active(handle)
            entered.__enter__()
        except Exception:
            entered = None
    try:
        yield
    finally:
        if entered is not None:
            with contextlib.suppress(Exception):
                entered.__exit__(None, None, None)


#: Whether this thread is inside a client's construction. Per thread, because a script may open
#: clients from several.
_building = threading.local()


@contextlib.contextmanager
def _building_a_client() -> Iterator[None]:
    """The stretch in which `sila2` is building a client, during which **commands are not
    measured**.

    What happens in there is the fetching of every feature definition the server implements --
    nine RPCs for one machine in the reference lab, and 54 of the 101 units one real run
    recorded before this. They are left to the gRPC instrumentation that records the same RPCs,
    under the connection they belong to; measuring them here as well would be one call counted
    twice, and an RPC path is a truer name for a transport than a command name is. What they
    cost stays visible as most of `sila2.connect`.

    Recognised by *when* they happen rather than by what they are called: a construction is a
    fact, whereas a name is a guess about what `sila2` fetches in there."""
    depth = getattr(_building, "depth", 0)
    _building.depth = depth + 1
    try:
        yield
    finally:
        _building.depth = depth


def _inside_a_client_build() -> bool:
    return getattr(_building, "depth", 0) > 0


def _remember(handle: Any) -> None:
    state = _state
    if state is None or handle is None:
        return
    with state.lock:
        state.open_handles.append(handle)


def _forget(handle: Any) -> None:
    state = _state
    if state is None or handle is None:
        return
    with state.lock:
        for index, known in enumerate(state.open_handles):
            if known is handle:
                del state.open_handles[index]
                return


def _error_type(exc: BaseException) -> str:
    """A failure's kind is the exception's class name. The SiLA2 fully qualified error identifier
    is deliberately not used: it would put an unbounded set of values in an indexed field."""
    return type(exc).__name__


# -- what counts as a unit ---------------------------------------------------------------


def _endpoint_from_call(args: tuple, kwargs: dict) -> tuple[Any, Any]:
    """The address a `SilaClient(...)` call is for, positional or named."""
    address = kwargs.get("address", args[0] if len(args) > 0 else None)
    port = kwargs.get("port", args[1] if len(args) > 1 else None)
    return address, port


def _endpoint_of(wrapper: Any) -> tuple[Any, Any]:
    """The address stashed on the client a command belongs to.

    The connection's unit is a command's **sibling, not its ancestor** (it closed at the start of
    the operation), so which machine was driven has to be on the command's unit too -- there is
    nothing to walk up to."""
    client = getattr(getattr(wrapper, "_parent_feature", None), "_parent_client", None)
    endpoint = getattr(client, _ENDPOINT_ATTR, None)
    if isinstance(endpoint, tuple) and len(endpoint) == 2:
        return endpoint
    return None, None


def _command_span(wrapper: Any) -> tuple[str, dict[str, Any]]:
    """The name and attributes of one command's unit.

    An object that cannot answer is not an error here: a record missing a name is worth more than
    an operation that failed because something wanted to describe it."""
    feature = getattr(getattr(wrapper, "_parent_feature", None), "_identifier", None)
    command_def = getattr(wrapper, "_wrapped_command", None)
    command = getattr(command_def, "_identifier", None)

    attributes: dict[str, Any] = {}
    if feature is not None:
        attributes[ATTR_FEATURE] = str(feature)
    if command is not None:
        attributes[ATTR_COMMAND] = str(command)
    # The fully qualified identifier carries the originator and the feature path, so two vendors'
    # identically named Features cannot be confused. It is the command's, not the feature's: the
    # feature's path is a prefix of it.
    fqi = getattr(command_def, "fully_qualified_identifier", None)
    if fqi is not None:
        attributes[ATTR_FQI] = str(fqi)
    address, port = _endpoint_of(wrapper)
    if address is not None:
        attributes[ATTR_ADDRESS] = str(address)
    if port is not None:
        attributes[ATTR_PORT] = port

    name = f"{SPAN_COMMAND_PREFIX}{feature or '?'}.{command or '?'}"
    return name, attributes


def _is_not_finished(state: _State, exc: BaseException) -> bool:
    """Whether `exc` means "the instrument is still working" (so the unit must stay open)."""
    not_finished = state.targets.not_finished
    return not_finished is not None and isinstance(exc, not_finished)


# -- the interventions -------------------------------------------------------------------


def _patch_client_init(state: _State) -> None:
    cls = state.targets.client
    original = cls.__init__

    @functools.wraps(original)
    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: N807
        address, port = _endpoint_from_call(args, kwargs)
        # Stashed before connecting, so a command can find it; a failed connection leaving the
        # address behind harms nothing.
        with contextlib.suppress(Exception):
            setattr(self, _ENDPOINT_ATTR, (address, port))
        attributes: dict[str, Any] = {}
        if address is not None:
            attributes[ATTR_ADDRESS] = str(address)
        if port is not None:
            attributes[ATTR_PORT] = port
        # Always measured: it is a cost every operation pays and cannot avoid (0.4-0.9 s against
        # the reference lab, 16% of one run's wall clock), it is not recoverable from anything
        # else the record holds, and it is what the gRPC instrumentation hangs its per-RPC
        # spans from.
        handle = _start(SPAN_CONNECT, attributes)
        try:
            # Everything `sila2` fetches to build the client happens in here, which is how
            # those commands are recognised (`_building_a_client`).
            with _active(handle), _building_a_client():
                original(self, *args, **kwargs)
        except BaseException as exc:
            _end(handle, error_type=_error_type(exc), message=str(exc))
            raise
        _end(handle)

    state.originals[(cls, "__init__")] = original
    cls.__init__ = __init__


def _patch_unobservable_call(state: _State) -> None:
    cls = state.targets.unobservable
    original = cls.__call__

    @functools.wraps(original)
    def __call__(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _inside_a_client_build():
            return original(self, *args, **kwargs)  # not a unit; see `_building_a_client`
        name, attributes = _command_span(self)
        handle = _start(name, attributes)
        try:
            with _active(handle):
                result = original(self, *args, **kwargs)
        except BaseException as exc:
            _end(handle, error_type=_error_type(exc), message=str(exc))
            raise
        _end(handle)
        return result

    state.originals[(cls, "__call__")] = original
    cls.__call__ = __call__


def _patch_observable_call(state: _State) -> None:
    cls = state.targets.observable
    original = cls.__call__

    @functools.wraps(original)
    def __call__(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _inside_a_client_build():
            return original(self, *args, **kwargs)  # not a unit; see `_building_a_client`
        name, attributes = _command_span(self)
        handle = _start(name, attributes)
        try:
            with _active(handle):
                instance = original(self, *args, **kwargs)
        except BaseException as exc:
            # The start itself failed, so no execution began; this unit is over.
            _end(handle, error_type=_error_type(exc), message=str(exc))
            raise
        if handle is not None:
            execution_uuid = getattr(instance, "execution_uuid", None)
            if execution_uuid is not None:
                _update(handle, {ATTR_EXECUTION_UUID: str(execution_uuid)})
            # The execution continues from here, so the unit stays open, tied to the instance that
            # can close it. If it cannot be tied on, the unit is still tracked: the handle for
            # closing it is lost, so `close_open` will end it as unsettled -- which is a truthful
            # record of what was observed, and better than a unit dropped while open.
            with contextlib.suppress(Exception):
                setattr(instance, _HANDLE_ATTR, handle)
            _remember(handle)
        return instance

    state.originals[(cls, "__call__")] = original
    cls.__call__ = __call__


def _patch_get_responses(state: _State) -> None:
    cls = state.targets.instance
    original = cls.get_responses

    @functools.wraps(original)
    def get_responses(self: Any, *args: Any, **kwargs: Any) -> Any:
        handle = getattr(self, _HANDLE_ATTR, None)
        if handle is None:  # started before instrumenting, or already closed (a second call)
            return original(self, *args, **kwargs)
        current = _state
        try:
            with _active(handle):
                result = original(self, *args, **kwargs)
        except BaseException as exc:
            if current is not None and _is_not_finished(current, exc):
                raise  # still running: leave it open rather than cut the measurement short
            _detach(self)
            _end(handle, error_type=_error_type(exc), message=str(exc))
            raise
        _detach(self)
        _end(handle)
        return result

    state.originals[(cls, "get_responses")] = original
    cls.get_responses = get_responses


def _detach(instance: Any) -> None:
    """Take the handle off a finished execution, so a unit is closed exactly once."""
    with contextlib.suppress(Exception):
        setattr(instance, _HANDLE_ATTR, None)
