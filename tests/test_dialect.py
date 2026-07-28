"""Tests for the labcode dialect validator (`labcode.dialect`, P5)."""

from __future__ import annotations

from labcode.dialect import validate_dialect


def _env(*modes: dict) -> dict:
    return {"processes": {"m": {"modes": list(modes)}}}


def test_reserved_id_view_field_is_rejected():
    # `_id` is labcode's implicit Object identity; a user type that declares it collides.
    workflow = {
        "types": {"Plate": {"domain": "object", "view": {"_id": {"type": "String"}}}},
        "processes": {},
    }
    result = validate_dialect(workflow, {"processes": {}})
    assert not result.ok
    assert any("_id" in e and "Plate" in e for e in result.errors)


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


# -- transport routes ----------------------------------------------------------


def _transport_env(route: dict) -> dict:
    return {"processes": {}, "transports": [route]}


def test_valid_transport_script_passes():
    result = validate_dialect(
        {},
        _transport_env({"transporter": "t", "from": "a", "to": "b",
                        "x-labcode": {"script": {"language": "python", "code": "move()"}}}),
    )
    assert result.ok
    assert not result.errors


def test_transport_script_language_must_be_python():
    result = validate_dialect(
        {},
        _transport_env({"transporter": "t", "from": "a", "to": "b",
                        "x-labcode": {"script": {"language": "r", "code": "x"}}}),
    )
    assert not result.ok
    assert any("python" in e for e in result.errors)


def test_scriptless_real_transport_warns():
    result = validate_dialect({}, _transport_env({"transporter": "t", "from": "a", "to": "b"}))
    assert result.ok
    assert any("no-op move" in w for w in result.warnings)


def test_same_spot_scriptless_transport_not_warned():
    # A same-spot move (from == to) is a physical no-op by design, not a missing script.
    result = validate_dialect({}, _transport_env({"transporter": "t", "from": "a", "to": "a"}))
    assert not any("no-op move" in w for w in result.warnings)
