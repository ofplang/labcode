"""Tests for the labcode execution backend (`labcode.backend`).

The code resolver is the heart of the dialect: it sources each operation's script from
the *environment* (`x-labcode.script`, labcode's (2)), falling back to the workflow's own
script ((1), v0 §22), else None (a typed-default no-op). These pin that resolution order
and that the factory builds a wired `SubprocessBackend`.
"""

from __future__ import annotations

from ofplang.run.simulator import SubprocessBackend

from labcode.backend import labcode_backend_factory, make_code_resolver

WF_DEF = {"script": {"language": "python", "code": "WF"}}


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


def test_factory_builds_subprocess_backend():
    env = {
        "time": {"unit": "second"},
        "devices": [{"id": "rack", "spots": ["slot"]}],
        "transporters": [{"id": "arm"}],
        "transports": [],
        "processes": {},
        "objective": {"kind": "makespan"},
    }
    backend = labcode_backend_factory(seconds_per_tick=0.001)(env)
    assert isinstance(backend, SubprocessBackend)
    backend.close()
