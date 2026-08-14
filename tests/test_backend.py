"""Tests for the labcode execution backend (`labcode.backend`).

The code resolver is the heart of the dialect: it sources each operation's script from
the *environment* (`x-labcode.script`, labcode's (2)), falling back to the workflow's own
script ((1), v0 §22), else None (a typed-default no-op). These pin that resolution order
and that the factory builds a wired `SubprocessBackend`.
"""

from __future__ import annotations

import textwrap

import pytest
from ofplang.run.simulator import DeviceComputationError, SubprocessBackend

from labcode.backend import (
    LabcodeBackend,
    labcode_backend_factory,
    make_code_resolver,
    make_transport_resolver,
)
from labcode.extension import DEFAULT_OP_TIMEOUT

WF_DEF = {"script": {"language": "python", "code": "WF"}}


class _RecordingHandle:
    """A never-finishing handle (poll -> None) for dispatch-only assertions."""

    returncode = None
    stderr = None
    stdin = None

    def poll(self):
        return None

    def terminate(self):
        pass


def _env(mode: dict) -> dict:
    return {"processes": {"measure": {"modes": [mode]}}}


def test_resolver_prefers_env_x_labcode():
    # (2) env x-labcode.script wins over (1) the workflow's own script.
    resolver = make_code_resolver(
        _env({"id": "v0", "x-labcode": {"script": {"language": "python", "code": "ENV"}}})
    )
    assert resolver("measure", "v0", {}, WF_DEF) == "ENV"


def test_resolver_falls_back_to_workflow_script():
    resolver = make_code_resolver(_env({"id": "v0"}))
    assert resolver("measure", "v0", {}, WF_DEF) == "WF"


def test_resolver_none_when_no_code():
    resolver = make_code_resolver(_env({"id": "v0"}))
    assert resolver("measure", "v0", {}, {}) is None
    assert resolver("measure", "v0", {}, None) is None
    assert resolver("unknown_process", "v0", {}, None) is None


def test_resolver_ignores_non_python_env_script():
    # A non-python env script is treated as absent, so resolution falls through to (1).
    resolver = make_code_resolver(
        _env({"id": "v0", "x-labcode": {"script": {"language": "r", "code": "X"}}})
    )
    assert resolver("measure", "v0", {}, WF_DEF) == "WF"


def test_factory_builds_labcode_backend():
    env = {
        "time": {"unit": "second"},
        "devices": [{"id": "rack", "spots": ["slot"]}],
        "transporters": [{"id": "arm"}],
        "transports": [],
        "processes": {},
        "objective": {"kind": "makespan"},
    }
    backend = labcode_backend_factory(seconds_per_tick=0.001)(env)
    assert isinstance(backend, LabcodeBackend)
    assert isinstance(backend, SubprocessBackend)
    backend.close()


# -- the operation timeout: three layers, outermost first ----------------------


def _timeout_env(**extension) -> dict:
    env = {
        "time": {"unit": "second"},
        "devices": [{"id": "rack", "spots": ["slot"]}],
        "transporters": [{"id": "arm"}],
        "transports": [],
        "processes": {},
        "objective": {"kind": "makespan"},
    }
    if extension:
        env["x-labcode"] = extension
    return env


def _built(env: dict, **kw) -> float | None:
    backend = labcode_backend_factory(seconds_per_tick=0.001, **kw)(env)
    try:
        return backend._op_timeout
    finally:
        backend.close()


def test_a_lab_that_says_nothing_still_gets_a_limit():
    # The point of the default: the hang that stops a run silently is exactly the one
    # nobody thought to bound.
    assert _built(_timeout_env()) == DEFAULT_OP_TIMEOUT


def test_the_environment_sets_the_limit():
    assert _built(_timeout_env(op_timeout=120)) == 120.0


def test_the_environment_can_ask_to_wait_forever():
    # A declared null is a decision on the record, unlike saying nothing.
    assert _built(_timeout_env(op_timeout=None)) is None


def test_the_caller_overrides_the_environment():
    assert _built(_timeout_env(op_timeout=120), op_timeout=30.0) == 30.0
    assert _built(_timeout_env(op_timeout=120), op_timeout=None) is None


def test_a_malformed_limit_falls_back_to_the_default():
    # The dialect front door rejects it; if one reaches execution anyway, falling back to
    # the default is the safe reading -- silently meaning "no limit" is not.
    assert _built(_timeout_env(op_timeout="soon")) == DEFAULT_OP_TIMEOUT


# -- transport resolution / dispatch -------------------------------------------


def test_transport_resolver_matches_route_exactly():
    env = {"transports": [
        {"transporter": "arm", "from": "a", "to": "b",
         "x-labcode": {"script": {"language": "python", "code": "MOVE"}}},
    ]}
    resolver = make_transport_resolver(env)
    assert resolver("arm", "a", "b") == "MOVE"
    assert resolver("arm", "a", "c") is None  # unmatched destination
    assert resolver("other", "a", "b") is None  # unmatched transporter


def test_transport_resolver_none_without_script():
    env = {"transports": [{"transporter": "arm", "from": "a", "to": "b"}]}
    assert make_transport_resolver(env)("arm", "a", "b") is None


# -- the sila2 flavor: the resolver wraps the code ------------------------------

CONNECTION = {"kind": "sila2", "host": "127.0.0.1", "port": 50053, "insecure": True}


def _sila2_mode_env(*, connected: bool = True) -> dict:
    device = {"id": "reader", "spots": ["stage"]}
    if connected:
        device["x-labcode"] = {"connection": CONNECTION}
    return {
        "devices": [device],
        "processes": {"measure": {"modes": [{
            "id": "v0",
            "devices": ["reader"],
            "x-labcode": {"script": {
                "language": "python", "flavor": "sila2",
                "code": 'return {"od": sila2_client.MeasureOD()}',
            }},
        }]}},
    }


def test_resolver_wraps_a_sila2_flavor_script():
    code = make_code_resolver(_sila2_mode_env())("measure", "v0", {}, None)
    assert code is not None
    assert "__lc_session" in code  # the clients are opened around it
    assert "('reader', '127.0.0.1', 50053, True)" in code
    assert "sila2_client.MeasureOD()" in code  # the author's line, untouched
    compile(f"def _f():\n{textwrap.indent(code, '    ')}", "<wrapped>", "exec")


def test_resolver_leaves_a_raw_script_unwrapped():
    env = _sila2_mode_env()
    env["processes"]["measure"]["modes"][0]["x-labcode"]["script"]["flavor"] = "raw"
    code = make_code_resolver(env)("measure", "v0", {}, None)
    assert code == 'return {"od": sila2_client.MeasureOD()}'


def test_resolver_fails_a_sila2_script_with_nothing_to_connect_to():
    # The front door rejects this; the backstop must still not run the code unwrapped (it
    # would fail on an undefined client) nor raise inside dispatch.
    code = make_code_resolver(_sila2_mode_env(connected=False))("measure", "v0", {}, None)
    assert code is not None
    assert code.startswith("raise RuntimeError(")
    assert "MeasureOD" not in code


def test_transport_resolver_wraps_a_sila2_flavor_script():
    env = {
        "transporters": [{"id": "arm", "x-labcode": {"connection": {**CONNECTION, "port": 50057}}}],
        "transports": [{"transporter": "arm", "from": "a", "to": "b", "x-labcode": {"script": {
            "language": "python", "flavor": "sila2",
            "code": "sila2_client.Pick(LocationSpecifier=from_spot)",
        }}}],
    }
    code = make_transport_resolver(env)("arm", "a", "b")
    assert code is not None
    assert "('arm', '127.0.0.1', 50057, True)" in code
    assert "sila2_client.Pick(LocationSpecifier=from_spot)" in code


def _sila2_transport_env(source: str, destination: str, **script) -> dict:
    """A route between two of three devices, only two of which have an address."""
    def device(identifier, spot, port=None):
        entry = {"id": identifier, "spots": [spot]}
        if port is not None:
            entry["x-labcode"] = {"connection": {**CONNECTION, "port": port}}
        return entry

    return {
        "devices": [
            device("plateloc", "stage", 50053),
            device("cycler", "block", 50055),
            device("station", "slot1"),
        ],
        "transporters": [
            {"id": "arm", "x-labcode": {"connection": {**CONNECTION, "port": 50057}}},
        ],
        "transports": [{
            "transporter": "arm", "from": source, "to": destination, "duration": 3,
            "x-labcode": {"script": {
                "language": "python", "flavor": "sila2",
                "code": "sila2_clients['cycler'].Lid.OpenLid()",
                **script,
            }},
        }],
    }


def _transport_code(source: str, destination: str, **script) -> str:
    code = make_transport_resolver(
        _sila2_transport_env(source, destination, **script)
    )("arm", source, destination)
    assert code is not None
    return code


def test_a_transport_that_asks_gets_the_devices_at_both_ends():
    # A transport activity occupies the source device, the destination device and the
    # transporter for its whole body (schedule §4.5), so all three are its to command --
    # that is what lets the move that needs a lid open be the one that opens it.
    code = _transport_code("plateloc.stage", "cycler.block", endpoints=True)
    assert "('plateloc', '127.0.0.1', 50053, True)" in code
    assert "('cycler', '127.0.0.1', 50055, True)" in code
    # The transporter stays first, so `sila2_client` -- "the first of them" (§1.6) -- is
    # still the machine that does the moving.
    order = [code.index(f"({name!r},") for name in ("arm", "plateloc", "cycler")]
    assert order == sorted(order)


def test_a_transport_gets_only_its_transporter_unless_it_asks():
    # The default: a move that drives nothing but its transporter pays for one connection,
    # and does not stop working because an instrument it merely hands a plate to is off.
    code = _transport_code("plateloc.stage", "cycler.block")
    assert "('arm', '127.0.0.1', 50057, True)" in code
    assert "('plateloc'," not in code
    assert "('cycler'," not in code
    # It still *holds* both ends, so reaching for one is answered with why, and with the
    # thing to add -- otherwise an off-by-default feature cannot be found.
    assert "unavailable={'plateloc': 'not_requested', 'cycler': 'not_requested'}" in code


def test_an_end_with_no_connection_is_not_connected_to_but_is_named():
    code = _transport_code("station.slot1", "cycler.block", endpoints=True)
    assert "('station'," not in code  # a holding location has no address to open
    # ...but the script hears why if it reaches for it, rather than getting a bare KeyError.
    assert "'station': 'no_connection'" in code


def test_a_route_within_one_device_connects_to_it_once():
    code = _transport_code("cycler.block", "cycler.block", endpoints=True)
    assert code.count("('cycler',") == 1


TRANSPORT_ENV = {
    "time": {"unit": "second"},
    "devices": [{"id": "s0", "spots": ["core"]}, {"id": "s1", "spots": ["core"]}],
    "transporters": [{"id": "t"}],
    "transports": [{"transporter": "t", "from": "s0.core", "to": "s1.core", "duration": 1,
                    "x-labcode": {"script": {"language": "python", "code": "MOVE"}}}],
    "processes": {},
    "objective": {"kind": "makespan"},
}


def _recording_backend(env):
    jobs: list = []

    def spawn(job):
        jobs.append(job)
        return _RecordingHandle()

    backend = LabcodeBackend(
        env, resolver=lambda *a: None, transport_resolver=make_transport_resolver(env),
        spawn=spawn, seconds_per_tick=0.001,
    )
    return backend, jobs


def test_dispatch_transport_launches_transport_child_with_view():
    backend, jobs = _recording_backend(TRANSPORT_ENV)
    backend.place("s0.core")
    backend.dispatch_transport("t", "s0.core", "s1.core", view={"fragile": True})
    assert len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "transport"
    assert job["code"] == "MOVE"
    assert job["inputs"] == {
        "from_spot": "s0.core", "to_spot": "s1.core", "transporter": "t",
        "view": {"fragile": True},
    }
    backend.close()


def test_dispatch_transport_without_script_is_timed():
    env = {**TRANSPORT_ENV,
           "transports": [{"transporter": "t", "from": "s0.core", "to": "s1.core", "duration": 1}]}
    backend, jobs = _recording_backend(env)
    backend.place("s0.core")
    backend.dispatch_transport("t", "s0.core", "s1.core")
    assert jobs == []  # no child launched -- a plain timed move
    backend.close()


# -- partial process outputs (SPECIFICATIONS.md §1.2) --------------------------

_MINIMAL_ENV = {
    "time": {"unit": "second"},
    "devices": [{"id": "d", "spots": ["s"]}],
    "transporters": [{"id": "a"}],
    "transports": [],
    "processes": {},
    "objective": {"kind": "makespan"},
}
# `read`: output `plate` (Plate, view {barcode, _id}) carried via objects.map + `od` (Float).
# The `_id` field reflects labcode's Object-identity rewrite (every Object view declares it).
_PLATE_SCHEMA = {
    "plate": {
        "kind": "record",
        "fields": {"barcode": {"kind": "primitive", "name": "String"},
                   "_id": {"kind": "primitive", "name": "String"}},
    },
    "od": {"kind": "primitive", "name": "Float"},
}
_MAP_DEF = {"objects": {"map": {"outputs.plate": "inputs.plate"}}}


def _resolve(pending_outputs, inputs):
    backend = LabcodeBackend(_MINIMAL_ENV, seconds_per_tick=0.001)
    backend._pending = {"outputs": pending_outputs}
    try:
        return backend._resolve_model("read", "m0", inputs, _PLATE_SCHEMA, _MAP_DEF)
    finally:
        backend.close()


def test_partial_empty_carries_object_and_defaults_the_rest():
    # `return {}`: the plate is carried by objects.map (view + `_id`), od defaults (0.0).
    out = _resolve({}, {"plate": {"barcode": "P001", "_id": "abc"}})
    assert out == {"plate": {"barcode": "P001", "_id": "abc"}, "od": 0.0}


def test_partial_merges_script_values_over_defaults():
    # `return {"od": 0.42}`: plate carried (with its `_id`); od taken from the script.
    out = _resolve({"od": 0.42}, {"plate": {"barcode": "P001", "_id": "abc"}})
    assert out == {"plate": {"barcode": "P001", "_id": "abc"}, "od": 0.42}


def test_undeclared_output_name_is_rejected():
    # `return {"pltae": 1}`: a name outside the declared outputs is an error (typo guard).
    with pytest.raises(DeviceComputationError):
        _resolve({"pltae": 1}, {"plate": {"barcode": "P001", "_id": "abc"}})
