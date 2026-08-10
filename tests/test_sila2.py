"""Tests for the `sila2` script flavor (`labcode.sila2`).

Two halves, tested apart: the **wrapper text** the resolver generates (it has to compile,
and to bind the clients the script expects), and the **session** that opens and closes the
connections (its job is that nothing stays open, whatever fails). Neither needs a network:
`connect` is replaced by a fake, and the wrapped code is run through the same
`run_python_script` the child uses.
"""

from __future__ import annotations

import pytest
from ofplang.run.simulator import DeviceComputationError, run_python_script

from labcode import sila2
from labcode.extension import Connection

PLATELOC = Connection(host="127.0.0.1", port=50053, insecure=True)
ARM = Connection(host="127.0.0.1", port=50057, insecure=True)


class _FakeClient:
    """Stands in for a `SilaClient`: records what was called on it, and whether it closed."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.closed = False
        self.calls: list[str] = []

    def StartCycle(self):  # noqa: N802 - a SiLA2 command name
        self.calls.append("StartCycle")
        return 7

    def close(self):
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
    """Replace `connect` with a recording fake, and hand back every client it opened."""
    opened: list[_FakeClient] = []

    def connect(host, port, *, insecure=False):
        client = _FakeClient(host, port)
        opened.append(client)
        return client

    monkeypatch.setattr(sila2, "connect", connect)
    return opened


# -- the wrapper text ----------------------------------------------------------


def test_wrap_binds_the_clients_and_runs_the_code(fake_connect):
    wrapped = sila2.wrap('return {"cycles": sila2_client.StartCycle()}', [("plateloc", PLATELOC)])
    assert run_python_script(wrapped, {}) == {"cycles": 7}
    assert [client.calls for client in fake_connect] == [["StartCycle"]]
    assert fake_connect[0].closed


def test_wrap_passes_the_declared_address(fake_connect):
    wrapped = sila2.wrap("pass", [("plateloc", PLATELOC)])
    run_python_script(wrapped, {})
    assert (fake_connect[0].host, fake_connect[0].port) == ("127.0.0.1", 50053)


def test_wrap_keeps_the_input_ports_available(fake_connect):
    # The wrapper is a function body like any other, so the operation's inputs are still
    # its parameters -- the script sees both its data and its client.
    wrapped = sila2.wrap('return {"od": plate["od"]}', [("plateloc", PLATELOC)])
    assert run_python_script(wrapped, {"plate": {"od": 0.42}}) == {"od": 0.42}


def test_wrap_binds_every_client_by_id_in_order(fake_connect):
    wrapped = sila2.wrap(
        "return {'ids': list(sila2_clients), 'first': sila2_client.port}",
        [("plateloc", PLATELOC), ("arm", ARM)],
    )
    assert run_python_script(wrapped, {}) == {"ids": ["plateloc", "arm"], "first": 50053}


def test_wrap_injects_connections_and_nothing_else(fake_connect):
    # The helper of §1.6.1 is reached by an import the script writes, not by injection, so
    # the wrapper must not bind it -- a script that does not import it does not have it.
    wrapped = sila2.wrap("pass", [("plateloc", PLATELOC)])
    assert "sila2_commands" not in wrapped


def test_a_script_may_import_the_helper_itself(fake_connect):
    # ...and importing it is all it takes: the child runs in an interpreter that has labcode.
    wrapped = sila2.wrap(
        "from labcode.sila2_commands import settle\nreturn {'ok': callable(settle)}",
        [("plateloc", PLATELOC)],
    )
    assert run_python_script(wrapped, {}) == {"ok": True}


def test_wrap_of_empty_code_still_compiles(fake_connect):
    # An empty body would leave the generated `with` without one.
    assert run_python_script(sila2.wrap("", [("plateloc", PLATELOC)]), {}) is None
    assert fake_connect[0].closed


def test_wrap_closes_the_client_when_the_script_raises(fake_connect):
    wrapped = sila2.wrap('raise ValueError("the instrument refused")', [("plateloc", PLATELOC)])
    with pytest.raises(DeviceComputationError):
        run_python_script(wrapped, {})
    assert fake_connect[0].closed


def test_failing_code_fails_the_operation():
    # How the resolver reports an operation it cannot run at all (no connection): as code,
    # since a resolver runs inside dispatch where raising would escape the whole run.
    with pytest.raises(DeviceComputationError):
        run_python_script(sila2.failing_code("nothing to connect to"), {})


# -- the session ---------------------------------------------------------------


def test_session_closes_everything_it_opened(fake_connect):
    with sila2.session([("plateloc", "127.0.0.1", 50053, True)]) as (clients, client):
        assert clients == {"plateloc": client}
    assert fake_connect[0].closed


def test_a_later_connection_failure_closes_the_earlier_ones(monkeypatch):
    # The failure this ordering exists for: connect the first machine, fail on the second,
    # and the first must not be left open.
    opened: list[_FakeClient] = []

    def connect(host, port, *, insecure=False):
        if port == 50057:
            raise OSError("connection refused")
        client = _FakeClient(host, port)
        opened.append(client)
        return client

    monkeypatch.setattr(sila2, "connect", connect)
    with pytest.raises(DeviceComputationError) as caught, sila2.session([
        ("plateloc", "127.0.0.1", 50053, True),
        ("arm", "127.0.0.1", 50057, True),
    ]):
        pytest.fail("the body must not run")
    assert "arm" in str(caught.value)  # named, so the operator knows which machine
    assert opened[0].closed


def test_session_needs_a_target():
    with pytest.raises(DeviceComputationError), sila2.session([]):
        pytest.fail("the body must not run")


def test_a_client_that_will_not_close_does_not_mask_the_result(monkeypatch):
    # An operation's outcome is what the script computed; a channel that would not shut
    # down cleanly must not replace it.
    class _Stubborn(_FakeClient):
        def close(self):
            raise OSError("channel stuck")

    monkeypatch.setattr(sila2, "connect", lambda host, port, **_: _Stubborn(host, port))
    wrapped = sila2.wrap('return {"ok": True}', [("plateloc", PLATELOC)])
    assert run_python_script(wrapped, {}) == {"ok": True}


# -- connect ------------------------------------------------------------------


def test_connect_refuses_tls():
    # TLS has nowhere in the schema to keep its credentials (§1.4). The front door rejects
    # it too; this is the same rule where a script could reach it directly.
    with pytest.raises(DeviceComputationError) as caught:
        sila2.connect("127.0.0.1", 50053, insecure=False)
    assert "TLS" in str(caught.value)


def test_connect_says_so_when_the_client_library_is_missing(monkeypatch):
    # The child runs in whatever interpreter launched it, which may not have the extra
    # installed; a raw ImportError from a subprocess is not a readable answer.
    import builtins

    real_import = builtins.__import__

    def no_sila2(name, *args, **kwargs):
        if name.startswith("sila2"):
            raise ImportError("No module named 'sila2'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sila2)
    with pytest.raises(DeviceComputationError) as caught:
        sila2.connect("127.0.0.1", 50053, insecure=True)
    assert "sila2" in str(caught.value)
    assert caught.value.code == "sila2_unavailable"


# -- picking the machines to connect to ----------------------------------------


def test_targets_keep_declaration_order_and_skip_the_unreachable():
    connections = {"plateloc": PLATELOC, "arm": ARM}
    assert sila2.targets_of(["arm", "station", "plateloc"], connections) == [
        ("arm", ARM), ("plateloc", PLATELOC),
    ]


def test_targets_do_not_repeat_a_machine():
    # A device listed twice must be connected to once.
    assert sila2.targets_of(["plateloc", "plateloc"], {"plateloc": PLATELOC}) == [
        ("plateloc", PLATELOC),
    ]


def test_targets_of_nothing_connectable_is_empty():
    assert sila2.targets_of(["station"], {"plateloc": PLATELOC}) == []
    assert sila2.targets_of(None, {"plateloc": PLATELOC}) == []
