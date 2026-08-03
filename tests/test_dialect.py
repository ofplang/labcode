"""Tests for the labcode dialect validator (`labcode.dialect`, P5)."""

from __future__ import annotations

from pathlib import Path

import yaml

from labcode.dialect import validate_dialect

EXAMPLES = Path(__file__).parent.parent / "examples"

#: A connection to the reference lab's plate sealer. `insecure` is true because TLS has
#: nowhere to keep its credentials in this version (see `test_tls_*` below).
CONNECTION = {"kind": "sila2", "host": "127.0.0.1", "port": 50053, "insecure": True}


def _env(*modes: dict) -> dict:
    return {"processes": {"m": {"modes": list(modes)}}}


def _sila2_script(**overrides) -> dict:
    """An `x-labcode` holding a `sila2`-flavored script."""
    script = {"language": "python", "code": "return {}", "flavor": "sila2"}
    script.update(overrides)
    return {"script": script}


def _device(identifier: str = "reader", **extra) -> dict:
    return {"id": identifier, "spots": ["stage"], **extra}


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


# -- where an x-labcode may appear ----------------------------------------------


def test_x_labcode_at_the_environment_root_is_rejected():
    # The natural guess for a document-wide default block -- and read by nobody, since
    # ofplang-schedule tolerates an x- key at every position without interpreting it.
    env = _env({"id": "v0"})
    env["x-labcode"] = {"probe": {"enabled": True}}
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("root" in e and "not supported" in e for e in result.errors)


def test_x_labcode_on_a_process_is_rejected():
    env = _env({"id": "v0"})
    env["processes"]["m"]["x-labcode"] = {"connection": CONNECTION}
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("processes.m" in e for e in result.errors)


def test_x_labcode_at_a_supported_position_is_not_reported():
    result = validate_dialect(
        {}, {"processes": {}, "devices": [_device(**{"x-labcode": {"connection": CONNECTION}})]}
    )
    assert result.ok


# -- closed shapes ----------------------------------------------------------------


def test_unknown_key_in_x_labcode_is_rejected():
    result = validate_dialect({}, _env({"id": "v0", "x-labcode": {"scrpit": {}}}))
    assert not result.ok
    assert any("unknown key 'scrpit'" in e for e in result.errors)


def test_probe_is_reported_as_not_supported_yet():
    # Silently ignoring a monitoring policy is the worst way to learn it does nothing.
    result = validate_dialect({}, _env({"id": "v0", "x-labcode": {"probe": {"enabled": True}}}))
    assert not result.ok
    assert any("probe is not supported" in e for e in result.errors)


def test_unknown_key_in_script_is_rejected():
    # `flavour` is the British spelling of a key that decides how the code is run; left
    # ignored it would run raw and fail on an undefined `client`.
    result = validate_dialect(
        {},
        _env({"id": "v0", "x-labcode": {
            "script": {"language": "python", "code": "x", "flavour": "sila2"}}}),
    )
    assert not result.ok
    assert any("unknown key 'flavour'" in e for e in result.errors)


def test_flavor_must_be_known():
    result = validate_dialect({}, _env({"id": "v0", "x-labcode": _sila2_script(flavor="opcua")}))
    assert not result.ok
    assert any("flavor" in e for e in result.errors)


def test_raw_flavor_is_the_default_and_passes():
    result = validate_dialect(
        {}, _env({"id": "v0", "x-labcode": {"script": {"language": "python", "code": "x"}}})
    )
    assert result.ok
    assert not any("flavor" in w for w in result.warnings)


def test_sila2_flavor_passes_without_complaint():
    # The backend wraps such a script with its clients open, so there is nothing to warn
    # about (Step A warned that the flavor was not yet interpreted; it now is).
    env = _env({"id": "v0", "devices": ["reader"], "x-labcode": _sila2_script()})
    env["devices"] = [_device(**{"x-labcode": {"connection": CONNECTION}})]
    result = validate_dialect({}, env)
    assert result.ok, result.errors
    assert not result.warnings


def test_reserved_client_input_port_is_rejected():
    # A script's inputs are bound as its function's parameters, so an input port named like
    # an injected client would be silently overwritten by it.
    workflow = {"processes": {"m": {"inputs": {"sila2_client": {"type": "Int"}}}}}
    env = _env({"id": "v0", "devices": ["reader"], "x-labcode": _sila2_script()})
    env["devices"] = [_device(**{"x-labcode": {"connection": CONNECTION}})]
    result = validate_dialect(workflow, env)
    assert not result.ok
    assert any("sila2_client" in e and "reserve" in e for e in result.errors)


def test_reserved_names_are_free_for_a_raw_script():
    # Nothing is injected into a raw script, so the names are the author's to use.
    workflow = {"processes": {"m": {"inputs": {"sila2_client": {"type": "Int"}}}}}
    env = _env({"id": "v0", "x-labcode": {"script": {"language": "python", "code": "x"}}})
    result = validate_dialect(workflow, env)
    assert result.ok, result.errors


# -- device / transporter connections ----------------------------------------------


def _device_env(*devices: dict) -> dict:
    return {"processes": {}, "devices": list(devices)}


def test_device_x_labcode_unknown_key_is_rejected():
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {"script": {}}})))
    assert not result.ok
    assert any("unknown key 'script'" in e for e in result.errors)


def test_connection_must_be_a_mapping():
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {"connection": "here"}})))
    assert not result.ok
    assert any("connection must be a mapping" in e for e in result.errors)


def test_connection_requires_a_host_and_a_port():
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {"connection": {}}})))
    assert not result.ok
    assert any("connection.host" in e for e in result.errors)
    assert any("connection.port" in e for e in result.errors)


def test_connection_port_must_not_be_a_boolean():
    # `bool` is an `int` in Python, so an unguarded check would take `true` as port 1.
    connection = {**CONNECTION, "port": True}
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {
        "connection": connection}})))
    assert not result.ok
    assert any("connection.port" in e for e in result.errors)


def test_connection_kind_must_be_sila2():
    connection = {**CONNECTION, "kind": "opcua"}
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {
        "connection": connection}})))
    assert not result.ok
    assert any("connection.kind" in e for e in result.errors)


def test_connection_unknown_key_is_rejected():
    connection = {**CONNECTION, "hostname": "127.0.0.1"}
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {
        "connection": connection}})))
    assert not result.ok
    assert any("unknown key 'hostname'" in e for e in result.errors)


# The two tests below fix a *limitation*, not a rule: when TLS gains the fields it needs
# (a root certificate and friends), both expectations have to be updated, not deleted.
def test_tls_is_rejected_when_insecure_is_not_declared():
    connection = {key: value for key, value in CONNECTION.items() if key != "insecure"}
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {
        "connection": connection}})))
    assert not result.ok
    assert any("insecure" in e and "defaults to false" in e for e in result.errors)


def test_tls_is_rejected_when_insecure_is_false():
    connection = {**CONNECTION, "insecure": False}
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {
        "connection": connection}})))
    assert not result.ok
    assert any("TLS is not supported" in e for e in result.errors)


def test_two_devices_of_the_same_id_with_a_connection_are_ambiguous():
    result = validate_dialect(
        {},
        _device_env(
            _device(**{"x-labcode": {"connection": CONNECTION}}),
            _device(**{"x-labcode": {"connection": {**CONNECTION, "port": 50054}}}),
        ),
    )
    assert not result.ok
    assert any("more than once" in e for e in result.errors)


def test_a_transporter_connection_passes():
    env = {"processes": {}, "transporters": [{"id": "arm", "x-labcode": {
        "connection": CONNECTION}}]}
    result = validate_dialect({}, env)
    assert result.ok, result.errors


# -- a sila2 script needs somewhere to connect ---------------------------------------


def test_sila2_mode_without_a_connected_device_is_rejected():
    env = _env({"id": "v0", "devices": ["reader"], "x-labcode": _sila2_script()})
    env["devices"] = [_device()]
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("none of its devices" in e for e in result.errors)


def test_sila2_mode_naming_an_undeclared_device_says_so():
    env = _env({"id": "v0", "devices": ["reader"], "x-labcode": _sila2_script()})
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("not declared in devices[]" in e for e in result.errors)


def test_sila2_mode_with_no_devices_is_rejected():
    result = validate_dialect({}, _env({"id": "v0", "x-labcode": _sila2_script()}))
    assert not result.ok
    assert any("no devices to connect to" in e for e in result.errors)


def test_sila2_mode_needs_only_one_connected_device():
    # A mode may drive several devices; one reachable client is enough to run.
    env = _env({"id": "v0", "devices": ["helper", "reader"], "x-labcode": _sila2_script()})
    env["devices"] = [_device("helper"), _device(**{"x-labcode": {"connection": CONNECTION}})]
    result = validate_dialect({}, env)
    assert result.ok, result.errors


def test_a_malformed_connection_is_not_also_reported_as_missing():
    # One fault, one error: the shape complaint is where the mistake is.
    env = _env({"id": "v0", "devices": ["reader"], "x-labcode": _sila2_script()})
    env["devices"] = [_device(**{"x-labcode": {"connection": {**CONNECTION, "port": 0}}})]
    result = validate_dialect({}, env)
    assert not result.ok
    assert not any("none of its devices" in e for e in result.errors)


def test_sila2_transport_with_a_connected_transporter_passes():
    env = _transport_env({"transporter": "arm", "from": "a", "to": "b",
                          "x-labcode": _sila2_script()})
    env["transporters"] = [{"id": "arm", "x-labcode": {"connection": CONNECTION}}]
    result = validate_dialect({}, env)
    assert result.ok, result.errors


def test_sila2_transport_without_a_transporter_connection_is_rejected():
    env = _transport_env({"transporter": "arm", "from": "a", "to": "b",
                          "x-labcode": _sila2_script()})
    env["transporters"] = [{"id": "arm"}]
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("declares no x-labcode.connection" in e for e in result.errors)


def test_sila2_transport_naming_an_undeclared_transporter_says_so():
    env = _transport_env({"transporter": "arm", "from": "a", "to": "b",
                          "x-labcode": _sila2_script()})
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("not declared in transporters[]" in e for e in result.errors)


# -- the shipped examples ------------------------------------------------------------


#: (workflow, environment) per shipped example. `sila2_seal` has two environments -- the
#: `flavor: sila2` one and the `raw` reference -- for the one workflow.
EXAMPLE_DOCUMENTS = (
    ("plate_line.workflow.yaml", "plate_line.env.yaml"),
    ("sila2_seal.workflow.yaml", "sila2_seal.env.yaml"),
    ("sila2_seal.workflow.yaml", "sila2_seal.wrapped.env.yaml"),
)


def test_the_examples_pass_the_validator():
    # Whatever the validator learns to reject, a shipped example must not be caught by it.
    for workflow_name, environment_name in EXAMPLE_DOCUMENTS:
        workflow = yaml.safe_load((EXAMPLES / workflow_name).read_text(encoding="utf-8"))
        environment = yaml.safe_load((EXAMPLES / environment_name).read_text(encoding="utf-8"))
        result = validate_dialect(workflow, environment)
        assert result.ok, (environment_name, result.errors)


def test_a_mode_without_an_id_is_located_by_its_position():
    # The examples leave `id` out (the runner fills it in later), so a message that said
    # "mode None" would point at every one of them equally.
    result = validate_dialect({}, _env({"devices": ["reader"], "x-labcode": "oops"}))
    assert not result.ok
    assert any("mode #0" in e for e in result.errors)
