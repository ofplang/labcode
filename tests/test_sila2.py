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

DEVICES = {"plateloc": PLATELOC}
TRANSPORTERS = {"arm": ARM}


def _held(*identifiers, connections=DEVICES):
    return [(identifier, connections) for identifier in identifiers]


def test_plan_keeps_the_order_held_and_says_why_a_machine_is_skipped():
    targets, unavailable = sila2.plan_clients(_held("plateloc", "station"))
    assert targets == [("plateloc", PLATELOC)]
    # Not an error: an operation may hold a device it does not drive. But not silent
    # either -- the reason travels, so a script that reaches for it hears it.
    assert unavailable == {"station": sila2.NO_CONNECTION}


def test_plan_looks_each_machine_up_in_its_own_map():
    # A transport holds its transporter and the devices at either end, and the two id
    # spaces are separate: neither map has to know about the other's machines.
    targets, unavailable = sila2.plan_clients(
        [("arm", TRANSPORTERS), ("plateloc", DEVICES), ("station", DEVICES)]
    )
    assert targets == [("arm", ARM), ("plateloc", PLATELOC)]
    assert unavailable == {"station": sila2.NO_CONNECTION}


def test_plan_does_not_repeat_a_machine():
    # A device held twice (a route whose ends are the same device, a mode listing it
    # twice) is connected to once, and the first mention decides the order.
    assert sila2.plan_clients(_held("plateloc", "plateloc"))[0] == [("plateloc", PLATELOC)]
    # An id in both maps resolves to whichever holds it first -- which is how the
    # transporter keeps its place at the head of the list.
    targets, _ = sila2.plan_clients([("arm", TRANSPORTERS), ("arm", {"arm": PLATELOC})])
    assert targets == [("arm", ARM)]


def test_plan_distinguishes_not_asked_for_from_no_address():
    # A transport holds both ends whether or not it asks to drive them, and the two
    # reasons a machine has no client are different facts about the environment.
    targets, unavailable = sila2.plan_clients(
        [("arm", TRANSPORTERS), ("plateloc", None), ("station", None)]
    )
    assert targets == [("arm", ARM)]
    assert unavailable == {
        "plateloc": sila2.NOT_REQUESTED, "station": sila2.NOT_REQUESTED,
    }


def test_plan_of_nothing_connectable_has_no_targets():
    targets, unavailable = sila2.plan_clients(_held("station"))
    assert targets == []
    assert unavailable == {"station": sila2.NO_CONNECTION}
    # Malformed ids are the validator's to report; here they are simply not machines.
    assert sila2.plan_clients([(None, DEVICES), ("", DEVICES)]) == ([], {})


# -- the machines an operation holds but has no client for ----------------------


def _wrap_with_station(code):
    """`code` wrapped for an operation holding two machines, one of them addressless."""
    return sila2.wrap(code, [("arm", ARM)], unavailable={"station": sila2.NO_CONNECTION})


def test_wrap_is_unchanged_when_every_machine_is_reachable():
    assert "unavailable" not in sila2.wrap("pass", [("plateloc", PLATELOC)])


def test_using_a_machine_with_no_client_says_why(fake_connect):
    wrapped = _wrap_with_station("sila2_clients['station'].LidController.OpenLid()")
    with pytest.raises(DeviceComputationError) as caught:
        run_python_script(wrapped, {})
    assert caught.value.code == "sila2_not_connected"
    assert "station" in str(caught.value)
    assert "x-labcode.connection" in str(caught.value)  # what to fix, not just what broke
    assert fake_connect[0].closed  # and the clients that did open still closed


def test_a_machine_the_route_did_not_ask_for_says_what_to_add(fake_connect):
    # An off-by-default feature that says only "there is no client" cannot be found. The
    # message has to name the thing to write.
    wrapped = sila2.wrap(
        "sila2_clients['cycler'].Lid.OpenLid()", [("arm", ARM)],
        unavailable={"cycler": sila2.NOT_REQUESTED},
    )
    with pytest.raises(DeviceComputationError) as caught:
        run_python_script(wrapped, {})
    assert caught.value.code == "sila2_endpoints_not_requested"
    assert "endpoints: true" in str(caught.value)


def test_a_machine_with_no_client_is_falsy(fake_connect):
    # So a script that can work either way needs no knowledge of how absence is spelled.
    wrapped = _wrap_with_station(
        "return {'station': bool(sila2_clients['station']), 'arm': bool(sila2_clients['arm'])}"
    )
    assert run_python_script(wrapped, {}) == {"station": False, "arm": True}


def test_an_id_the_operation_does_not_hold_fails_at_once(fake_connect):
    # A typo, not a machine: turning it into a falsy stand-in would let it survive until
    # something odd happened later.
    wrapped = _wrap_with_station("sila2_clients['statoin']")
    with pytest.raises(DeviceComputationError) as caught:
        run_python_script(wrapped, {})
    assert "statoin" in str(caught.value)
    assert "'arm', 'station'" in str(caught.value)  # and what it does hold


def test_the_clients_are_otherwise_an_ordinary_mapping(fake_connect):
    wrapped = _wrap_with_station(
        "return {'in': 'station' in sila2_clients, 'get': sila2_clients.get('station'),"
        " 'ids': list(sila2_clients)}"
    )
    assert run_python_script(wrapped, {}) == {"in": False, "get": None, "ids": ["arm"]}
