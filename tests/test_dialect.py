"""Tests for the labcode dialect validator (`labcode.dialect`, P5)."""

from __future__ import annotations

from labcode.dialect import validate_dialect


def _env(*modes: dict) -> dict:
    return {"processes": {"m": {"modes": list(modes)}}}


def test_valid_env_script_passes():
    result = validate_dialect(
        {"processes": {}},
        _env({"id": "v0", "x-labcode": {"script": {"language": "python", "code": "return {}"}}}),
    )
    assert result.ok
    assert not result.errors


def test_language_must_be_python():
    result = validate_dialect(
        {}, _env({"id": "v0", "x-labcode": {"script": {"language": "r", "code": "x"}}})
    )
    assert not result.ok
    assert any("python" in e for e in result.errors)


def test_code_must_be_string():
    result = validate_dialect(
        {}, _env({"id": "v0", "x-labcode": {"script": {"language": "python", "code": 123}}})
    )
    assert not result.ok
    assert any("code" in e for e in result.errors)


def test_x_labcode_must_be_mapping():
    result = validate_dialect({}, _env({"id": "v0", "x-labcode": "oops"}))
    assert not result.ok


def test_exclusivity_of_workflow_and_env_scripts():
    workflow = {"processes": {"m": {"script": {"language": "python", "code": "WF"}}}}
    env = _env({"id": "v0", "x-labcode": {"script": {"language": "python", "code": "ENV"}}})
    result = validate_dialect(workflow, env)
    assert not result.ok
    assert any("exclusive" in e for e in result.errors)


def test_typed_default_is_a_warning_not_an_error():
    # A process with neither (1) nor (2) will run as a typed-default no-op: allowed, warned.
    result = validate_dialect({"processes": {}}, _env({"id": "v0", "duration": 3}))
    assert result.ok
    assert any("typed-default" in w for w in result.warnings)
