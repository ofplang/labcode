"""The client a `flavor: lads` script is handed: one LADS functional unit (SPECIFICATIONS.md §1.10).

A SiLA2 client is typed by its server's features, so a `sila2` script reads well with nothing in
between. A raw OPC UA client is not: driving a LADS instrument through asyncua directly means
resolving namespaces, building NodeIds and wrapping every argument in a Variant, and a script
that did so would be mostly plumbing (G34). So a `lads` script is handed a `LadsUnit` instead --
a thin, synchronous view of one **functional unit** of one **device** on a LADS server, built
only on what the LADS companion specification (OPC 30500, LADS 1.0.0) defines:

- programs: ``FunctionalUnitState.StartProgram`` and the ``ProgramManager.ResultSet`` a run ends
  in (`start_program`, `run_program`);
- the unit's state machine: ``Stop`` / ``Abort`` / ``Clear`` and ``CurrentState`` (`stop`,
  `abort`, `clear`, `reset`, `state`);
- functions: a cover's ``Open`` / ``Close``, a controller's ``TargetValue`` (`cover`,
  `write_target`);
- anything else by browse path (`read`, `write`, `child`), and the asyncua client itself
  (`raw`) for what none of that covers.

A server may add to the standard, and one addition is used where present: a ``LastError``
variable on the unit -- the reason the last call or run failed, which a StatusCode alone does not
carry -- is quoted in the failures raised here. labcode does not depend on it.

asyncua is imported only by `labcode.lads.connect`; this module receives its client and its
``ua`` module, so labcode stays importable without the ``lads`` extra.
"""

from __future__ import annotations

from typing import Any

from ofplang.run.simulator import DeviceComputationError

from labcode import lads_commands

#: The namespaces a LADS server's information model lives in.
LADS_URI = "http://opcfoundation.org/UA/LADS/"
DI_URI = "http://opcfoundation.org/UA/DI/"

#: Numeric identifiers in those namespaces (stable for LADS 1.0.0 / DI 1.04).
DI_DEVICE_SET = 5001
LADS_DEVICE_TYPE = 1002
LADS_FUNCTIONAL_UNIT_TYPE = 1003

#: How far up a type hierarchy to look when asking "is this a LADS device / unit". A vendor type
#: derives from the LADS one in a handful of steps; the bound only stops a malformed hierarchy
#: from looping.
_MAX_TYPE_DEPTH = 16


class LadsUnit:
    """One functional unit of a device on a LADS OPC UA server, driven synchronously."""

    def __init__(self, client: Any, node: Any, *, ua: Any, device: str, unit: str, lads_ns: int):
        self._client = client
        self._node = node
        self._ua = ua
        self._lads_ns = lads_ns
        #: ``device/unit``, the browse names this unit was found by -- used to name it in failures.
        self.name = f"{device}/{unit}"

    # -- what a script may reach directly --------------------------------------------------

    @property
    def raw(self) -> Any:
        """The asyncua synchronous client, for whatever this class does not cover."""
        return self._client

    @property
    def node(self) -> Any:
        """The functional unit's node."""
        return self._node

    def close(self) -> None:
        self._client.disconnect()

    def __repr__(self) -> str:
        return f"<LADS unit {self.name}>"

    # -- browsing ------------------------------------------------------------------------------

    def child(self, path: str) -> Any:
        """The node at `path` below the unit: browse names joined by ``/``
        (``"FunctionSet/SealingTemperature/TargetValue"``).

        Each step matches a browse *name*, in whichever namespace it lives -- LADS members, DI
        members and a vendor's own nodes sit side by side under one unit, and a script should
        not have to know which is which. A name that matches two children is an error rather
        than a guess."""
        node = self._node
        walked: list[str] = []
        for name in (segment for segment in path.split("/") if segment):
            walked.append(name)
            matches = [
                child for child in node.get_children() if child.read_browse_name().Name == name
            ]
            if not matches:
                raise DeviceComputationError(
                    f"{self.name}: no node {'/'.join(walked)!r} under the unit",
                    code="lads_node_not_found",
                )
            if len(matches) > 1:
                raise DeviceComputationError(
                    f"{self.name}: {'/'.join(walked)!r} names {len(matches)} nodes under the unit",
                    code="lads_node_ambiguous",
                )
            node = matches[0]
        return node

    def read(self, path: str) -> Any:
        """The value of the variable at `path` (see `child`)."""
        return self.child(path).read_value()

    def write(self, path: str, value: Any) -> None:
        """Write `value` to the variable at `path`, as the variable's own data type. A write the
        server refuses (a set-point out of range, say) fails the operation."""
        node = self.child(path)
        variant = self._ua.Variant(value, node.read_data_type_as_variant_type())
        try:
            node.write_value(variant)
        except self._ua.UaStatusCodeError as exc:
            raise self._call_failed(f"writing {path}", exc) from exc

    def write_target(self, function: str, value: float) -> None:
        """Set a control function's set-point: ``FunctionSet/<function>/TargetValue``."""
        self.write(f"FunctionSet/{function}/TargetValue", float(value))

    def state(self, owner: str = "FunctionalUnitState") -> str:
        """The current state of the state machine at `owner` -- the unit's own by default
        (``Stopped``, ``Running``, ``Aborted``, ...), or a function's
        (``FunctionSet/Door/CoverState``)."""
        return str(self.read(f"{owner}/CurrentState").Text)

    def last_error(self) -> str | None:
        """The unit's ``LastError`` variable, where the server provides one (not part of LADS)."""
        matches = [c for c in self._node.get_children() if c.read_browse_name().Name == "LastError"]
        if len(matches) != 1:
            return None
        value = matches[0].read_value()
        return str(value) if value else None

    # -- calling ---------------------------------------------------------------------------------

    def call(self, owner: str, method: str, *arguments: Any) -> Any:
        """Call `method` (a LADS method of the object at `owner`) with Variant `arguments`.

        A Bad StatusCode fails the operation, quoting the unit's ``LastError`` where there is
        one: the StatusCode says *that* the server refused, the message says *why*."""
        target = self.child(owner) if owner else self._node
        try:
            return target.call_method(self._ua.QualifiedName(method, self._lads_ns), *arguments)
        except self._ua.UaStatusCodeError as exc:
            raise self._call_failed(f"{owner or 'unit'}.{method}", exc) from exc

    def _call_failed(self, what: str, exc: Exception) -> DeviceComputationError:
        reason = self.last_error()
        detail = f": {reason}" if reason else ""
        return DeviceComputationError(
            f"{self.name}: {what} was refused ({type(exc).__name__}){detail}",
            code="lads_call_failed",
        )

    # -- programs ------------------------------------------------------------------------------

    def start_program(self, template_id: str, **properties: Any) -> str:
        """Start the program `template_id` with `properties` (stringified into the KeyValueType[]
        LADS takes) and return its run id. Returns once the server has started the run; see
        `run_program` to wait for it."""
        ua = self._ua
        key_values = [
            ua.KeyValueType(Key=key, Value=str(value)) for key, value in properties.items()
        ]
        run_id = self.call(
            "FunctionalUnitState",
            "StartProgram",
            ua.Variant(template_id, ua.VariantType.String),
            ua.Variant(key_values, ua.VariantType.ExtensionObject),
            ua.Variant("", ua.VariantType.String),
            ua.Variant("", ua.VariantType.String),
            ua.Variant([], ua.VariantType.ExtensionObject),
        )
        return str(run_id)

    def run_program(
        self,
        template_id: str,
        *,
        timeout: float | None = lads_commands.DEFAULT_TIMEOUT,
        poll: float = lads_commands.DEFAULT_POLL,
        **properties: Any,
    ) -> dict[str, str]:
        """Start `template_id` and wait for its result (`labcode.lads_commands.run_program`)."""
        return lads_commands.run_program(
            self, template_id, timeout=timeout, poll=poll, **properties
        )

    def results(self) -> list[Any]:
        """The result nodes currently in the unit's ``ProgramManager/ResultSet``."""
        result_set = self.child("ProgramManager/ResultSet")
        object_class = self._ua.NodeClass.Object
        return [c for c in result_set.get_children() if c.read_node_class() == object_class]

    # -- the unit's state machine --------------------------------------------------------------

    def stop(self, *, timeout: float | None = lads_commands.TRANSITION_TIMEOUT) -> str:
        """``Stop``, and wait until the unit has settled; returns the state it settled in."""
        self.call("FunctionalUnitState", "Stop")
        return lads_commands.wait_for_state(self, timeout=timeout, label=f"{self.name} Stop")

    def abort(self, *, timeout: float | None = lads_commands.TRANSITION_TIMEOUT) -> str:
        """``Abort``, and wait until the unit has settled (normally ``Aborted``)."""
        self.call("FunctionalUnitState", "Abort")
        return lads_commands.wait_for_state(self, timeout=timeout, label=f"{self.name} Abort")

    def clear(self, *, timeout: float | None = lads_commands.TRANSITION_TIMEOUT) -> str:
        """``Clear`` (from ``Aborted``), and wait until the unit has settled (normally
        ``Stopped``)."""
        self.call("FunctionalUnitState", "Clear")
        return lads_commands.wait_for_state(self, timeout=timeout, label=f"{self.name} Clear")

    def reset(self, *, timeout: float | None = lads_commands.TRANSITION_TIMEOUT) -> str:
        """Abort then Clear: the LADS counterpart of a SiLA2 ``Reset``. Fails the operation
        unless the unit ends ``Stopped``."""
        self.abort(timeout=timeout)
        settled = self.clear(timeout=timeout)
        if settled != "Stopped":
            raise self._settled_wrong("reset", settled, "Stopped")
        return settled

    # -- functions -------------------------------------------------------------------------------

    def cover(
        self, name: str, action: str, *, timeout: float | None = lads_commands.TRANSITION_TIMEOUT
    ) -> str:
        """``Open`` or ``Close`` the cover function `name` (a lid, a door) and wait until it has
        settled. Fails the operation unless it ends ``Opened`` / ``Closed`` as asked -- a server
        that could not carry the movement out returns the cover to where it was."""
        if action not in ("Open", "Close"):
            raise ValueError(f"cover action must be 'Open' or 'Close', not {action!r}")
        owner = f"FunctionSet/{name}/CoverState"
        self.call(owner, action)
        wanted = "Opened" if action == "Open" else "Closed"
        settled = lads_commands.wait_for_state(
            self, owner=owner, timeout=timeout, label=f"{self.name} {name}.{action}"
        )
        if settled != wanted:
            raise self._settled_wrong(f"{name}.{action}", settled, wanted)
        return settled

    def _settled_wrong(self, what: str, settled: str, wanted: str) -> DeviceComputationError:
        reason = self.last_error()
        detail = f": {reason}" if reason else ""
        return DeviceComputationError(
            f"{self.name}: {what} settled in {settled!r}, not {wanted!r}{detail}",
            code="lads_call_failed",
        )


# -- finding the unit on a server ------------------------------------------------------------


def open_unit(client: Any, ua: Any, *, device: str | None, unit: str | None) -> LadsUnit:
    """The functional unit a connection names, on an already connected `client`.

    `device` and `unit` are browse names (``PlateLoc`` / ``Sealer``). Either may be None, meaning
    "the only one there is": a server with one device holding one unit needs neither, and one
    with several is asked to say which (G35) rather than having one picked for it."""
    namespaces = list(client.get_namespace_array())
    if LADS_URI not in namespaces or DI_URI not in namespaces:
        raise DeviceComputationError(
            "the server does not expose the LADS information model (no LADS / DI namespace)",
            code="lads_not_a_lads_server",
        )
    lads_ns, di_ns = namespaces.index(LADS_URI), namespaces.index(DI_URI)

    device_set = client.get_node(ua.NodeId(DI_DEVICE_SET, di_ns))
    devices = _instances(client, ua, device_set, ua.NodeId(LADS_DEVICE_TYPE, lads_ns))
    device_name, device_node = _pick(devices, device, "device", "the server's DeviceSet")

    unit_sets = [
        c for c in device_node.get_children() if c.read_browse_name().Name == "FunctionalUnitSet"
    ]
    if len(unit_sets) != 1:
        raise DeviceComputationError(
            f"device {device_name!r} has no FunctionalUnitSet", code="lads_target_not_found"
        )
    units = _instances(client, ua, unit_sets[0], ua.NodeId(LADS_FUNCTIONAL_UNIT_TYPE, lads_ns))
    unit_name, unit_node = _pick(units, unit, "unit", f"device {device_name!r}")
    return LadsUnit(client, unit_node, ua=ua, device=device_name, unit=unit_name, lads_ns=lads_ns)


def _instances(client: Any, ua: Any, parent: Any, type_id: Any) -> dict[str, Any]:
    """The object children of `parent` whose type is `type_id` or derives from it, by browse
    name."""
    found: dict[str, Any] = {}
    for child in parent.get_children(nodeclassmask=ua.NodeClass.Object):
        if _derives_from(client, ua, child.read_type_definition(), type_id):
            found[child.read_browse_name().Name] = child
    return found


def _derives_from(client: Any, ua: Any, type_id: Any, base: Any) -> bool:
    current = type_id
    for _ in range(_MAX_TYPE_DEPTH):
        if current is None:
            return False
        if current == base:
            return True
        supertypes = client.get_node(current).get_references(
            refs=ua.ObjectIds.HasSubtype, direction=ua.BrowseDirection.Inverse
        )
        current = supertypes[0].NodeId if supertypes else None
    return False


def _pick(found: dict[str, Any], wanted: str | None, what: str, where: str) -> tuple[str, Any]:
    if wanted is not None:
        if wanted not in found:
            raise DeviceComputationError(
                f"no LADS {what} {wanted!r} in {where} (it has {sorted(found)})",
                code="lads_target_not_found",
            )
        return wanted, found[wanted]
    if len(found) == 1:
        return next(iter(found.items()))
    if not found:
        raise DeviceComputationError(f"no LADS {what} in {where}", code="lads_target_not_found")
    raise DeviceComputationError(
        f"{where} has {len(found)} LADS {what}s ({sorted(found)}); name one with "
        f"`connection.{what}`",
        code="lads_ambiguous_target",
    )
