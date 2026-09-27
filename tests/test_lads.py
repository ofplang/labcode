"""Tests for the `lads` script flavor (`labcode.lads`).

The same two halves as the `sila2` flavor's tests: the **wrapper text** the resolver generates
(it has to compile and bind `lads_clients` / `lads_client`), and the **session** that opens and
closes the units (nothing stays open, whatever fails). `connect` is replaced by a fake and the
wrapped code runs through the child's own `run_python_script`, so no network is needed. The
shared machinery (`labcode.connections`) is exercised through the `sila2` tests as well; what is
checked here is what differs -- the target shape with `device` / `unit`, the protocol's names in
messages and codes, and a machine of the other protocol being held without a client.
"""

from __future__ import annotations

import builtins

import pytest
from ofplang.run.simulator import DeviceComputationError, run_python_script

from labcode import lads
from labcode.extension import Connection

PLATELOC = Connection(host="127.0.0.1", port=4842, insecure=True, kind="lads")
CYCLER = Connection(
    host="127.0.0.1", port=4844, insecure=True, kind="lads", device="ThermalCycler", unit="Cycler"
)
SILA2_ARM = Connection(host="127.0.0.1", port=50057, insecure=True)


class _FakeUnit:
    """Stands in for a `LadsUnit`: records how it was opened, what was run, and whether it
    closed."""

    def __init__(self, host, port, device, unit):
        self.address = (host, port, device, unit)
        self.closed = False
        self.runs: list[str] = []

    def run_program(self, template_id, **properties):
        self.runs.append(template_id)
        return {"Outcome": "Completed", **{k: str(v) for k, v in properties.items()}}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
    opened: list[_FakeUnit] = []

    def connect(host, port, *, insecure=False, device=None, unit=None):
        client = _FakeUnit(host, port, device, unit)
        opened.append(client)
        return client

    monkeypatch.setattr(lads, "connect", connect)
    return opened


# -- the wrapper text ------------------------------------------------------------------------


def test_wrap_binds_the_units_and_runs_the_code(fake_connect):
    code = 'return {"outcome": lads_client.run_program("StartCycle")["Outcome"]}'
    wrapped = lads.wrap(code, [("plateloc", PLATELOC)])
    assert run_python_script(wrapped, {}) == {"outcome": "Completed"}
    assert fake_connect[0].runs == ["StartCycle"]
    assert fake_connect[0].closed


def test_wrap_passes_the_declared_device_and_unit(fake_connect):
    run_python_script(lads.wrap("pass", [("plateloc", PLATELOC), ("cycler", CYCLER)]), {})
    assert [unit.address for unit in fake_connect] == [
        ("127.0.0.1", 4842, None, None),
        ("127.0.0.1", 4844, "ThermalCycler", "Cycler"),
    ]


def test_wrap_binds_every_unit_by_id_in_order(fake_connect):
    wrapped = lads.wrap(
        "return {'ids': list(lads_clients)}", [("cycler", CYCLER), ("plateloc", PLATELOC)]
    )
    assert run_python_script(wrapped, {}) == {"ids": ["cycler", "plateloc"]}


def test_wrap_generates_the_session_of_this_module():
    wrapped = lads.wrap("pass", [("plateloc", PLATELOC)])
    assert wrapped.startswith("from labcode.lads import session as __lc_session\n")
    assert "as (lads_clients, lads_client):" in wrapped
    assert "('plateloc', '127.0.0.1', 4842, True, None, None)" in wrapped


def test_wrap_closes_the_unit_when_the_script_raises(fake_connect):
    with pytest.raises(DeviceComputationError):
        run_python_script(lads.wrap("raise RuntimeError('boom')", [("plateloc", PLATELOC)]), {})
    assert fake_connect[0].closed


# -- the session -------------------------------------------------------------------------------


def test_a_failed_connection_is_named_and_closes_the_earlier_ones(monkeypatch):
    opened: list[_FakeUnit] = []

    def connect(host, port, *, insecure=False, device=None, unit=None):
        if port == 4844:
            raise ConnectionRefusedError("refused")
        client = _FakeUnit(host, port, device, unit)
        opened.append(client)
        return client

    monkeypatch.setattr(lads, "connect", connect)
    targets = [
        ("plateloc", "127.0.0.1", 4842, True, None, None),
        ("cycler", "127.0.0.1", 4844, True, None, None),
    ]
    with pytest.raises(DeviceComputationError) as caught, lads.session(targets):
        pass
    assert caught.value.code == "lads_connect_failed"
    assert "'cycler' at 127.0.0.1:4844" in str(caught.value)
    assert opened[0].closed


def test_session_needs_a_target():
    with pytest.raises(DeviceComputationError) as caught, lads.session([]):
        pass
    assert caught.value.code == "lads_no_target"


def test_connect_refuses_security():
    with pytest.raises(DeviceComputationError) as caught:
        lads.connect("127.0.0.1", 4842, insecure=False)
    assert caught.value.code == "lads_tls_unsupported"


def test_connect_says_so_when_the_client_library_is_missing(monkeypatch):
    real_import = builtins.__import__

    def no_asyncua(name, *args, **kwargs):
        if name == "asyncua" or name.startswith("asyncua."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_asyncua)
    with pytest.raises(DeviceComputationError) as caught:
        lads.connect("127.0.0.1", 4842, insecure=True)
    assert caught.value.code == "lads_unavailable"


def test_the_endpoint_is_opc_tcp():
    assert lads.endpoint_url("10.0.0.5", 4840) == "opc.tcp://10.0.0.5:4840/"


# -- one protocol per script (G40) ---------------------------------------------------------------


def test_a_machine_of_the_other_protocol_is_held_without_a_client():
    targets, unavailable = lads.plan_clients(
        [("arm", {"arm": SILA2_ARM}), ("plateloc", {"plateloc": PLATELOC})]
    )
    assert [identifier for identifier, _ in targets] == ["plateloc"]
    assert unavailable == {"arm": lads.OTHER_PROTOCOL}


def test_using_a_machine_of_the_other_protocol_says_why(fake_connect):
    wrapped = lads.wrap(
        "lads_clients['arm'].run_program('Transfer')",
        [("plateloc", PLATELOC)],
        unavailable={"arm": lads.OTHER_PROTOCOL},
    )
    with pytest.raises(DeviceComputationError) as caught:
        run_python_script(wrapped, {})
    assert caught.value.code == "lads_other_protocol"
    assert "no LADS client for 'arm'" in str(caught.value)
    assert "not a LADS one" in str(caught.value)


def test_a_sila2_script_holds_a_lads_machine_without_a_client_too():
    from labcode import sila2

    targets, unavailable = sila2.plan_clients(
        [("arm", {"arm": SILA2_ARM}), ("plateloc", {"plateloc": PLATELOC})]
    )
    assert [identifier for identifier, _ in targets] == ["arm"]
    assert unavailable == {"plateloc": sila2.connections.OTHER_PROTOCOL}
