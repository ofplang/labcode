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


def test_x_labcode_at_the_environment_root_holds_probing_defaults():
    env = _device_env(_device(**{"x-labcode": {"connection": CONNECTION}}))
    env["x-labcode"] = {"probe": {"enabled": True}}
    result = validate_dialect({}, env)
    assert result.ok, result.errors
    assert not result.warnings


def test_a_connection_at_the_environment_root_is_rejected():
    # An address belongs to the machine that has it; only defaults live at the root.
    env = _device_env(_device())
    env["x-labcode"] = {"connection": CONNECTION}
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("unknown key 'connection'" in e for e in result.errors)


def test_the_root_holds_the_operation_timeout():
    # One value for the whole lab: how long any operation may run before labcode stops
    # waiting for it. A positive number of real seconds, or null for no limit at all.
    for value in (7200, 0.5, None):
        env = _device_env(_device())
        env["x-labcode"] = {"op_timeout": value}
        result = validate_dialect({}, env)
        assert result.ok, result.errors


def test_a_nonsensical_operation_timeout_is_rejected():
    # Zero is not "no limit" (null is), and neither a string nor an infinity is a wait.
    for value in (0, -1, "3600", True, float("inf"), float("nan")):
        env = _device_env(_device())
        env["x-labcode"] = {"op_timeout": value}
        result = validate_dialect({}, env)
        assert not result.ok, value
        assert any("op_timeout" in e for e in result.errors)


def test_an_operation_timeout_on_a_machine_is_rejected():
    # Per-machine limits are deliberately not a thing: a script that knows what it waits
    # for says so itself (`settle`), and the outer net is one number for the lab.
    env = _device_env(_device(**{"x-labcode": {"op_timeout": 60}}))
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("unknown key 'op_timeout'" in e for e in result.errors)


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


def test_probe_is_not_a_key_on_a_process_mode():
    # Probing is about a machine, not about one of the things it does.
    result = validate_dialect({}, _env({"id": "v0", "x-labcode": {"probe": {"enabled": True}}}))
    assert not result.ok
    assert any("unknown key 'probe'" in e for e in result.errors)


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


def test_the_helper_module_name_is_not_reserved():
    # `sila2_commands` is imported by a script, never injected into it, so it takes no name
    # away from the author -- an input port may be called that.
    workflow = {"processes": {"m": {"inputs": {"sila2_commands": {"type": "Int"}}}}}
    env = _env({"id": "v0", "devices": ["reader"], "x-labcode": _sila2_script()})
    env["devices"] = [_device(**{"x-labcode": {"connection": CONNECTION}})]
    result = validate_dialect(workflow, env)
    assert result.ok, result.errors


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


# -- probing policies ------------------------------------------------------------


def _probed_device(**probe) -> dict:
    return _device(**{"x-labcode": {"connection": CONNECTION, "probe": probe}})


def test_a_probed_device_passes():
    result = validate_dialect({}, _device_env(_probed_device(enabled=True, timeout=2, interval=60)))
    assert result.ok, result.errors
    assert not result.warnings


def test_interval_may_be_once_or_zero():
    for interval in ("once", 0):
        result = validate_dialect({}, _device_env(_probed_device(enabled=True, interval=interval)))
        assert result.ok, (interval, result.errors)


def test_probe_unknown_key_is_rejected():
    result = validate_dialect({}, _device_env(_probed_device(enabled=True, evrey=1)))
    assert not result.ok
    assert any("unknown key 'evrey'" in e for e in result.errors)


def test_probe_field_types_are_checked():
    for probe, expected in (
        ({"enabled": "yes"}, "probe.enabled"),
        ({"enabled": True, "timeout": 0}, "probe.timeout"),
        ({"enabled": True, "timeout": True}, "probe.timeout"),
        ({"enabled": True, "interval": -1}, "probe.interval"),
        ({"enabled": True, "interval": "hourly"}, "probe.interval"),
    ):
        result = validate_dialect({}, _device_env(_device(**{"x-labcode": {
            "connection": CONNECTION, "probe": probe}})))
        assert not result.ok, probe
        assert any(expected in e for e in result.errors), (probe, result.errors)


def test_a_probed_machine_needs_an_address():
    # Enabled by its own policy, but there is nowhere to probe.
    result = validate_dialect({}, _device_env(_device(**{"x-labcode": {
        "probe": {"enabled": True}}})))
    assert not result.ok
    assert any("no x-labcode.connection" in e and "probe" in e for e in result.errors)


def test_the_root_enabling_probing_also_needs_addresses():
    # The chosen strictness: `enabled: true` at the root reaches every machine, so a
    # holding device with no connection has to be excluded on purpose.
    reader = _device("reader", **{"x-labcode": {"connection": CONNECTION}})
    env = _device_env(reader, _device("rack"))
    env["x-labcode"] = {"probe": {"enabled": True}}
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("device 'rack'" in e for e in result.errors)

    env["devices"][1]["x-labcode"] = {"probe": {"enabled": False}}
    excluded = validate_dialect({}, env)
    assert excluded.ok, excluded.errors
    assert not excluded.warnings  # an explicit exclusion is not a dormant policy


def test_a_transporter_is_probed_the_same_way():
    env = {"processes": {}, "transporters": [{"id": "arm", "x-labcode": {
        "probe": {"enabled": True}}}]}
    result = validate_dialect({}, env)
    assert not result.ok
    assert any("transporter 'arm'" in e and "probe" in e for e in result.errors)


def test_a_probe_policy_that_nothing_enables_is_warned():
    # It does nothing -- which is what `enabled` defaulting to false means -- but writing
    # a policy reads as asking for one, so say it out loud.
    result = validate_dialect({}, _device_env(_probed_device(interval=60)))
    assert result.ok, result.errors
    assert any("not enabled" in w for w in result.warnings)


def test_a_root_policy_that_reaches_nothing_is_warned():
    env = _device_env(_device())  # no connection anywhere, so nothing can be probed
    env["x-labcode"] = {"probe": {"interval": 60}}
    result = validate_dialect({}, env)
    assert result.ok, result.errors
    assert any("no machine is probed" in w for w in result.warnings)


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


def test_sila2_transport_does_not_need_its_ends_to_be_connected():
    # A transport is handed the clients of the devices at either end as well as its
    # transporter, but an end that declares no connection is not an error: a plain holding
    # location has no address, and a route through one is ordinary. Only the transporter --
    # the machine that does the moving, and `sila2_client` -- has to be reachable.
    env = _transport_env({"transporter": "arm", "from": "station.slot1", "to": "reader.stage",
                          "x-labcode": _sila2_script()})
    env["transporters"] = [{"id": "arm", "x-labcode": {"connection": CONNECTION}}]
    env["devices"] = [_device("station", spots=["slot1"]), _device("reader")]
    result = validate_dialect({}, env)
    assert result.ok, result.errors


# -- asking for the clients of the devices at either end (`endpoints`) ----------------


def _endpoints_env(endpoints, *, flavor: str = "sila2", connected: bool = True) -> dict:
    env = _transport_env({
        "transporter": "arm", "from": "station.slot1", "to": "reader.stage",
        "x-labcode": _sila2_script(flavor=flavor, endpoints=endpoints),
    })
    env["transporters"] = [{"id": "arm", "x-labcode": {"connection": CONNECTION}}]
    reader = {"x-labcode": {"connection": CONNECTION}} if connected else {}
    env["devices"] = [_device("station", spots=["slot1"]), _device("reader", **reader)]
    return env


def test_a_transport_may_ask_for_its_endpoint_clients():
    result = validate_dialect({}, _endpoints_env(True))
    assert result.ok, result.errors
    assert not result.warnings


def test_endpoints_false_states_the_default():
    result = validate_dialect({}, _endpoints_env(False))
    assert result.ok, result.errors
    assert not result.warnings


def test_endpoints_must_be_a_boolean():
    result = validate_dialect({}, _endpoints_env("yes"))
    assert not result.ok
    assert any("endpoints must be a boolean" in e for e in result.errors)


def test_a_raw_script_cannot_ask_for_clients_it_will_not_be_given():
    # A raw script is handed no clients at all, so the request cannot be honoured -- and its
    # author is expecting something that will not happen.
    result = validate_dialect({}, _endpoints_env(True, flavor="raw"))
    assert not result.ok
    assert any("only a 'sila2' script is handed clients" in e for e in result.errors)


def test_endpoints_with_no_addressable_end_is_a_warning():
    # The route still works through its transporter, and an environment written before its
    # instruments have addresses is a legitimate intermediate state -- as with `probe`.
    result = validate_dialect({}, _endpoints_env(True, connected=False))
    assert result.ok, result.errors
    assert any("neither end of the route" in w for w in result.warnings)


def test_a_process_mode_may_not_ask_for_endpoints():
    # A mode's machines are the ones it lists; there are no ends of a route to ask about.
    result = validate_dialect({}, _env({"id": "v0", "devices": ["reader"],
                                        "x-labcode": _sila2_script(endpoints=True)}))
    assert not result.ok
    assert any("unknown key 'endpoints'" in e for e in result.errors)


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


# -- replenishment routes ---------------------------------------------------
#
# A refill is described where the pair is: the (replenisher, device) route carries the
# procedure, the machine carries only its address -- the same division transporters and
# transports have.


def _refill_env(script: dict | None = None, **entry_extra) -> dict:
    entry = {"replenisher": "dispenser", "device": "reader", "duration": 4, **entry_extra}
    if script is not None:
        entry["x-labcode"] = {"script": script}
    return {
        "devices": [_device()],
        "replenishers": [{"id": "dispenser"}],
        "replenishments": [entry],
        "processes": {},
    }


def test_a_refill_script_is_accepted_on_its_route():
    result = validate_dialect({"processes": {}}, _refill_env(
        {"language": "python", "code": "import time\ntime.sleep(1)"}
    ))
    assert result.ok, result.errors
    assert not result.warnings


def test_a_route_with_no_script_is_warned_about():
    """A legitimate thing to write -- an operator tops the stock up while the schedule
    waits for them -- and an easy thing to write by accident, so it is said out loud,
    exactly as a scriptless transport route is."""
    result = validate_dialect({"processes": {}}, _refill_env())
    assert result.ok, result.errors
    assert any("command nothing" in w for w in result.warnings)


def test_a_sila2_refill_script_is_refused_for_now():
    """A sila2 script is handed clients, and which machine's clients a refill should
    receive -- the replenisher's, or both ends' as a transport may ask for -- is not
    settled. An error says so; running the script without clients would not."""
    result = validate_dialect({"processes": {}}, _refill_env(
        {"language": "python", "code": "return {}", "flavor": "sila2"}
    ))
    assert not result.ok
    assert any("'sila2' is not supported for a refill" in e for e in result.errors)


def test_a_refill_script_may_not_ask_for_endpoints():
    """`endpoints` is a transport's request (which end's clients to open); a refill route
    has no such key, so asking is a typo."""
    result = validate_dialect({"processes": {}}, _refill_env(
        {"language": "python", "code": "pass", "endpoints": True}
    ))
    assert not result.ok
    assert any("endpoints" in e for e in result.errors)


def test_an_unknown_key_on_a_refill_route_extension_is_rejected():
    env = _refill_env({"language": "python", "code": "pass"})
    env["replenishments"][0]["x-labcode"]["connection"] = CONNECTION
    result = validate_dialect({"processes": {}}, env)
    assert not result.ok
    assert any("connection" in e for e in result.errors)


def test_a_replenisher_may_declare_where_it_is_reached():
    """The machine carries the address, the route carries the procedure."""
    env = _refill_env({"language": "python", "code": "pass"})
    env["replenishers"][0]["x-labcode"] = {"connection": CONNECTION}
    result = validate_dialect({"processes": {}}, env)
    assert result.ok, result.errors


def test_a_replenisher_is_probed_when_it_declares_a_connection_and_a_policy():
    from labcode.probe import probe_targets

    env = _refill_env({"language": "python", "code": "pass"})
    env["replenishers"][0]["x-labcode"] = {
        "connection": CONNECTION,
        "probe": {"enabled": True},
    }
    assert validate_dialect({"processes": {}}, env).ok
    assert [t.identifier for t in probe_targets(env)] == ["dispenser"]


def test_an_x_labcode_somewhere_nothing_reads_still_says_where_it_belongs():
    env = _refill_env({"language": "python", "code": "pass"})
    env["replenishments"][0]["nested"] = {"x-labcode": {}}
    result = validate_dialect({"processes": {}}, env)
    assert not result.ok
    assert any("replenishments[]" in e for e in result.errors)
