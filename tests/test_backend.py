"""Tests for the labcode execution backend (`labcode.backend`).

The code resolver is the heart of the dialect: it sources each operation's script from
the *environment* (`x-labcode.script`, labcode's (2)), falling back to the workflow's own
script ((1), v0 §22), else None (a typed-default no-op). These pin that resolution order
and that the factory builds a wired `SubprocessBackend`.
"""

from __future__ import annotations

from ofplang.run.simulator import SubprocessBackend

from labcode.backend import (
    LabcodeBackend,
    labcode_backend_factory,
    make_code_resolver,
    make_transport_resolver,
)

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
