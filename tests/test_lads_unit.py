"""Tests for the unit a `lads` script is handed (`labcode.lads_unit`) and its waits
(`labcode.lads_commands`).

A LADS server is replaced by a small tree of fake nodes shaped like the LADS information model
-- DeviceSet, a device, its FunctionalUnitSet, a unit with its state machine, program manager
and functions -- while the values travelling through it are the real `asyncua.ua` types, so what
is checked is how the real library's arguments are built and its answers read. The fake server
carries out a method as soon as it is called; the waits are exercised against transient states
set by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

ua = pytest.importorskip("asyncua.ua")

from ofplang.run.simulator import DeviceComputationError  # noqa: E402

from labcode import lads_commands  # noqa: E402
from labcode.lads_unit import (  # noqa: E402
    DI_DEVICE_SET,
    DI_URI,
    LADS_DEVICE_TYPE,
    LADS_FUNCTIONAL_UNIT_TYPE,
    LADS_URI,
    open_unit,
)

LADS_NS, DI_NS, VENDOR_NS = 2, 3, 4


@dataclass
class _KeyValue:
    """The shape of the `KeyValueType` a real client generates from a server's types."""

    Key: str
    Value: str


class _Ua:
    """`asyncua.ua`, plus the generated `KeyValueType` (which only exists after a client has
    loaded a server's data type definitions)."""

    KeyValueType = _KeyValue

    def __getattr__(self, name: str) -> Any:
        return getattr(ua, name)


class _Node:
    def __init__(self, name: str, *, node_class=None, value=None, type_def=None, variant_type=None):
        self.name = name
        self.node_class = node_class if node_class is not None else ua.NodeClass.Object
        self.value = value
        self.type_def = type_def
        self.variant_type = variant_type
        self.children: list[_Node] = []
        self.methods: dict[str, Any] = {}
        self.writes: list[Any] = []
        self.refuse_write: int | None = None

    def add(self, child: _Node) -> _Node:
        self.children.append(child)
        return child

    # -- the asyncua SyncNode surface LadsUnit uses --------------------------------------------

    def get_children(self, nodeclassmask=None):
        if nodeclassmask is None:
            return list(self.children)
        return [c for c in self.children if c.node_class == nodeclassmask]

    def read_browse_name(self):
        return ua.QualifiedName(self.name, VENDOR_NS)

    def read_value(self):
        return self.value

    def read_node_class(self):
        return self.node_class

    def read_type_definition(self):
        return self.type_def

    def read_data_type_as_variant_type(self):
        return self.variant_type

    def write_value(self, variant):
        if self.refuse_write is not None:
            raise ua.UaStatusCodeError(self.refuse_write)
        self.writes.append(variant)
        self.value = variant.Value

    def call_method(self, qualified_name, *arguments):
        assert qualified_name.NamespaceIndex == LADS_NS
        return self.methods[qualified_name.Name](*arguments)


def _variable(name: str, value=None, variant_type=None) -> _Node:
    return _Node(name, node_class=ua.NodeClass.Variable, value=value, variant_type=variant_type)


def _state_machine(name: str, state: str) -> _Node:
    machine = _Node(name)
    machine.add(_variable("CurrentState", ua.LocalizedText(state, "en")))
    return machine


def _set_state(machine: _Node, state: str) -> None:
    machine.children[0].value = ua.LocalizedText(state, "en")


class _Server:
    """A LADS server with one device holding one unit, carrying methods out at once."""

    def __init__(
        self, *, devices=("PlateLoc",), units=("Sealer",), outcome: str | None = "Completed"
    ):
        self.outcome = outcome
        self.device_set = _Node("DeviceSet")
        self.disconnected = False
        self.unit_nodes: dict[str, _Node] = {}
        for device_name in devices:
            device = self.device_set.add(
                _Node(device_name, type_def=ua.NodeId(LADS_DEVICE_TYPE, LADS_NS))
            )
            unit_set = device.add(_Node("FunctionalUnitSet"))
            for unit_name in units:
                self.unit_nodes[unit_name] = unit_set.add(self._unit(unit_name))
        # Something else in the DeviceSet that is not a LADS device, to be ignored.
        self.device_set.add(_Node("DeviceFeatures", type_def=ua.NodeId(58, 0)))

    def _unit(self, name: str) -> _Node:
        unit = _Node(name, type_def=ua.NodeId(LADS_FUNCTIONAL_UNIT_TYPE, LADS_NS))
        self.state = unit.add(_state_machine("FunctionalUnitState", "Stopped"))
        self.last_error = unit.add(_variable("LastError", ""))
        manager = unit.add(_Node("ProgramManager"))
        self.result_set = manager.add(_Node("ResultSet"))
        functions = unit.add(_Node("FunctionSet"))
        lid = functions.add(_Node("Lid"))
        self.cover = lid.add(_state_machine("CoverState", "Opened"))
        heater = functions.add(_Node("SealingTemperature"))
        self.target = heater.add(_variable("TargetValue", 175.0, ua.VariantType.Double))
        self.runs: list[tuple[str, dict[str, str]]] = []
        self.state.methods.update(
            StartProgram=self._start_program,
            Stop=lambda: _set_state(self.state, "Stopped"),
            Abort=lambda: _set_state(self.state, "Aborted"),
            Clear=lambda: _set_state(self.state, "Stopped"),
        )
        self.cover.methods.update(
            Open=lambda: _set_state(self.cover, "Opened"),
            Close=lambda: _set_state(self.cover, "Closed"),
        )
        return unit

    def _start_program(self, template, properties, job, task, samples):
        assert template.VariantType == ua.VariantType.String
        assert properties.VariantType == ua.VariantType.ExtensionObject
        run_id = f"{template.Value}-1"
        values = {pair.Key: pair.Value for pair in properties.Value}
        self.runs.append((template.Value, values))
        result = self.result_set.add(_Node(run_id))
        result.add(_variable("DeviceProgramRunId", run_id))
        recorded = [_KeyValue(k, v) for k, v in values.items()]
        if self.outcome is not None:
            recorded.append(_KeyValue("Outcome", self.outcome))
        result.add(_variable("Properties", recorded))
        return run_id

    # -- the asyncua sync Client surface ---------------------------------------------------------

    def get_namespace_array(self):
        return ["http://opcfoundation.org/UA/", "urn:server", LADS_URI, DI_URI, "urn:vendor"]

    def get_node(self, node_id):
        if node_id == ua.NodeId(DI_DEVICE_SET, DI_NS):
            return self.device_set
        return _TypeNode()

    def disconnect(self):
        self.disconnected = True


class _TypeNode:
    """A type node with no supertype: the fake LADS types are the roots of their hierarchies."""

    def get_references(self, refs=None, direction=None):
        return []


def _unit(server: _Server | None = None, **names):
    server = server or _Server()
    return server, open_unit(server, _Ua(), device=names.get("device"), unit=names.get("unit"))


# -- finding the unit ------------------------------------------------------------------------------


def test_the_only_unit_is_found_without_naming_it():
    _server, unit = _unit()
    assert unit.name == "PlateLoc/Sealer"


def test_a_named_unit_is_found_among_several():
    _server, unit = _unit(_Server(units=("Sealer", "Heater")), unit="Heater")
    assert unit.name == "PlateLoc/Heater"


def test_several_units_and_none_named_asks_for_a_name():
    with pytest.raises(DeviceComputationError) as caught:
        _unit(_Server(units=("Sealer", "Heater")))
    assert caught.value.code == "lads_ambiguous_target"
    assert "connection.unit" in str(caught.value)


def test_a_device_that_is_not_there_names_what_is():
    with pytest.raises(DeviceComputationError) as caught:
        _unit(device="Centrifuge")
    assert caught.value.code == "lads_target_not_found"
    assert "['PlateLoc']" in str(caught.value)


def test_a_server_without_lads_is_refused():
    server = _Server()
    server.get_namespace_array = lambda: ["http://opcfoundation.org/UA/"]
    with pytest.raises(DeviceComputationError) as caught:
        open_unit(server, _Ua(), device=None, unit=None)
    assert caught.value.code == "lads_not_a_lads_server"


# -- reading, writing, calling --------------------------------------------------------------------


def test_paths_are_browse_names_in_any_namespace():
    _server, unit = _unit()
    assert unit.read("FunctionSet/SealingTemperature/TargetValue") == 175.0
    assert unit.state() == "Stopped"
    assert unit.state("FunctionSet/Lid/CoverState") == "Opened"


def test_a_missing_path_is_named():
    _server, unit = _unit()
    with pytest.raises(DeviceComputationError) as caught:
        unit.read("FunctionSet/Door/CoverState")
    assert caught.value.code == "lads_node_not_found"
    assert "'FunctionSet/Door'" in str(caught.value)


def test_a_set_point_is_written_as_the_variable_s_own_type():
    server, unit = _unit()
    unit.write_target("SealingTemperature", 180)
    assert server.target.writes[-1].VariantType == ua.VariantType.Double
    assert server.target.value == 180.0


def test_a_refused_write_quotes_the_last_error():
    server, unit = _unit()
    server.target.refuse_write = ua.StatusCodes.BadInvalidArgument
    server.last_error.value = "SealingTemperature must be >= 0"
    with pytest.raises(DeviceComputationError) as caught:
        unit.write_target("SealingTemperature", -1)
    assert caught.value.code == "lads_call_failed"
    assert str(caught.value).endswith(": SealingTemperature must be >= 0")


def test_a_refused_call_quotes_the_last_error():
    server, unit = _unit()

    def refuse(*arguments):
        server.last_error.value = "requires an item at location 'plateloc.stage'"
        raise ua.UaStatusCodeError(ua.StatusCodes.BadInvalidState)

    server.state.methods["StartProgram"] = refuse
    with pytest.raises(DeviceComputationError) as caught:
        unit.start_program("StartCycle")
    assert caught.value.code == "lads_call_failed"
    assert "requires an item" in str(caught.value)


def test_without_a_last_error_the_status_code_is_enough():
    server, unit = _unit()
    server.unit_nodes["Sealer"].children.remove(server.last_error)
    assert unit.last_error() is None


# -- programs ----------------------------------------------------------------------------------


def test_run_program_passes_the_properties_and_returns_the_result():
    server, unit = _unit()
    result = unit.run_program("Peel", BeginPeelLocation=1, AdhesionTime=2)
    assert server.runs == [("Peel", {"BeginPeelLocation": "1", "AdhesionTime": "2"})]
    assert result == {"BeginPeelLocation": "1", "AdhesionTime": "2", "Outcome": "Completed"}


def test_a_failed_run_fails_the_operation_with_the_reason():
    server, unit = _unit(_Server(outcome="Failed"))
    server.last_error.value = "Validate must be executed before StartRun in this mock"
    with pytest.raises(DeviceComputationError) as caught:
        unit.run_program("StartRun")
    assert caught.value.code == "lads_program_failed"
    assert "Validate must be executed" in str(caught.value)


def test_without_an_outcome_an_aborted_unit_is_a_failure():
    server, unit = _unit(_Server(outcome=None))
    assert unit.run_program("StartCycle") == {}
    _set_state(server.state, "Aborted")
    with pytest.raises(DeviceComputationError) as caught:
        unit.run_program("StartCycle")
    assert caught.value.code == "lads_program_failed"


def test_a_result_is_recognised_by_its_browse_name_when_it_carries_no_run_id():
    server, unit = _unit()
    result = server.result_set.add(_Node("Legacy-7"))
    result.add(_variable("Properties", [_KeyValue("Outcome", "Completed")]))
    found = lads_commands.wait_for_result(unit, "Legacy-7", label="x", timeout=0)
    assert found == {"Outcome": "Completed"}


def test_a_result_that_never_appears_times_out_without_aborting(monkeypatch):
    server, unit = _unit()
    clock = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr(lads_commands.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(lads_commands.time, "sleep", lambda seconds: None)
    with pytest.raises(DeviceComputationError) as caught:
        lads_commands.wait_for_result(unit, "never", label="PlateLoc/Sealer StartCycle", timeout=5)
    assert caught.value.code == "lads_program_timeout"
    assert "nothing was aborted" in str(caught.value)
    assert unit.state() == "Stopped"


# -- the state machine and functions ------------------------------------------------------------


def test_stop_waits_out_a_transient_state(monkeypatch):
    server, unit = _unit()
    states = iter(["Stopping", "Stopping", "Stopped"])

    def stop():
        _set_state(server.state, next(states))

    server.state.methods["Stop"] = stop
    original_read = server.state.children[0].read_value
    server.state.children[0].read_value = lambda: (stop(), original_read())[1]  # type: ignore[method-assign]
    monkeypatch.setattr(lads_commands.time, "sleep", lambda seconds: None)
    assert unit.stop() == "Stopped"


def test_a_transition_that_never_settles_times_out(monkeypatch):
    server, unit = _unit()
    server.state.methods["Clear"] = lambda: _set_state(server.state, "Clearing")
    clock = iter([0.0, 0.0, 700.0])
    monkeypatch.setattr(lads_commands.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(lads_commands.time, "sleep", lambda seconds: None)
    with pytest.raises(DeviceComputationError) as caught:
        unit.clear()
    assert caught.value.code == "lads_transition_timeout"


def test_reset_is_abort_then_clear():
    server, unit = _unit()
    called: list[str] = []
    server.state.methods["Abort"] = lambda: (
        called.append("Abort"),
        _set_state(server.state, "Aborted"),
    )
    server.state.methods["Clear"] = lambda: (
        called.append("Clear"),
        _set_state(server.state, "Stopped"),
    )
    assert unit.reset() == "Stopped"
    assert called == ["Abort", "Clear"]


def test_a_cover_that_returns_to_where_it_was_fails_the_operation():
    server, unit = _unit()
    assert unit.cover("Lid", "Close") == "Closed"
    server.cover.methods["Open"] = lambda: None  # the movement failed; the lid stayed closed
    server.last_error.value = "failed to update access state"
    with pytest.raises(DeviceComputationError) as caught:
        unit.cover("Lid", "Open")
    assert caught.value.code == "lads_call_failed"
    assert "settled in 'Closed', not 'Opened'" in str(caught.value)


def test_close_disconnects():
    server, unit = _unit()
    unit.close()
    assert server.disconnected
