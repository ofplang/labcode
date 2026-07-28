"""labcode's implicit Object identity: the reserved view key ``_id``.

labcode gives every Object a stable, value-layer identity carried in its view under the
reserved key ``_id`` (a String). This is a *dialect* feature layered on portable v0: the
workflow the user writes has no ``_id``; labcode injects it. The mechanism, all here:

* **Type rewrite** (`inject_id_field`): add ``_id: {type: String}`` to the ``view`` of
  every ``domain: object`` type. ``_id`` is legal v0 (a normal String view field), so
  the rewritten workflow validates and schedules unchanged; the runner's closed-shape
  view conformance then treats ``_id`` as an ordinary declared field. labcode runs this
  rewritten document (in memory -- ``run_workflow`` accepts a mapping).
* **Reserved-collision check** (`reserved_collisions`): a user type that *already*
  declares ``_id`` is an error (the dialect front door rejects it), so labcode never
  clobbers a user field.
* **Boundary minting** (`inject_boundary_ids`): a whole-workflow Object *input* enters at
  the run boundary; labcode mints its ``_id`` (unless the boundary already carries one,
  so a result boundary fed back round-trips) and fills any other declared view field with
  a typed default, so the seeded value conforms.
* **Create minting / map carry** (`stamp_object_ids`): applied to each process op's
  produced outputs -- an ``objects.create`` Object gets a freshly minted ``_id``; an
  ``objects.map`` Object carries its input's ``_id`` (identity preserved). See
  `labcode.backend`.

Identity then propagates for free: ``objects.map`` and transport carry the whole view.
Ids come from a swappable `labcode.idgen.IdGenerator`, keyed by *provenance* (a node
instance + port, or a boundary port) so they are reproducible regardless of the backend's
wall-clock completion order.
"""

from __future__ import annotations

import copy

from labcode.idgen import IdGenerator

# The reserved view field name. Not user-declarable (see `reserved_collisions`).
RESERVED_ID = "_id"

# Typed defaults per v0 primitive, to fill a boundary Object input's non-`_id` view
# fields the user omitted (a view value must carry exactly its declared fields).
_PRIMITIVE_DEFAULTS = {"Bool": False, "Int": 0, "Float": 0.0, "String": ""}


def object_type_names(workflow: dict) -> set[str]:
    """The names of the workflow's ``domain: object`` types."""
    types = workflow.get("types") or {}
    return {
        name
        for name, spec in types.items()
        if isinstance(spec, dict) and spec.get("domain") == "object"
    }


def reserved_collisions(workflow: dict) -> list[str]:
    """Object type names whose ``view`` already declares the reserved ``_id`` key
    (an authoring error -- labcode owns ``_id``). Returned sorted for a stable message."""
    types = workflow.get("types") or {}
    hits = [
        name
        for name in object_type_names(workflow)
        if isinstance((types[name] or {}).get("view"), dict)
        and RESERVED_ID in types[name]["view"]
    ]
    return sorted(hits)


def inject_id_field(workflow: dict) -> dict:
    """Return a deep copy of `workflow` with ``_id: {type: String}`` added to the
    ``view`` of every ``domain: object`` type (creating ``view`` if absent).

    Assumes no reserved collision (`reserved_collisions` is checked first at the front
    door); a type that already has ``_id`` is left as-is rather than overwritten."""
    out = copy.deepcopy(workflow)
    types = out.setdefault("types", {})
    for name in object_type_names(out):
        spec = types[name]
        view = spec.get("view")
        if not isinstance(view, dict):
            view = {}
            spec["view"] = view
        view.setdefault(RESERVED_ID, {"type": "String"})
    return out


def _view_schema(workflow: dict, type_name: str) -> dict:
    """The ``{field: descriptor}`` view schema of `type_name` (empty if none)."""
    spec = (workflow.get("types") or {}).get(type_name) or {}
    view = spec.get("view")
    return view if isinstance(view, dict) else {}


def _default_field(descriptor: object) -> object:
    """A typed default for a view-field descriptor (``{type: <name>}``). A primitive
    yields its default; an Array yields ``[]``; anything else falls back to ``None``."""
    type_name = descriptor.get("type") if isinstance(descriptor, dict) else None
    if type_name in _PRIMITIVE_DEFAULTS:
        return _PRIMITIVE_DEFAULTS[type_name]
    if type_name == "Array":
        return []
    return None


def entry_object_inputs(workflow: dict) -> dict[str, str]:
    """Map each Object-bearing entry (whole-workflow) input port -> its type name.

    The entry composite's inputs whose declared type is a ``domain: object`` type are
    the run-boundary Objects; a boundary must place each on a spot and (with this
    feature) supply its ``_id``."""
    entry = workflow.get("entry")
    processes = workflow.get("processes") or {}
    proc = processes.get(entry) or {}
    inputs = proc.get("inputs") or {}
    objects = object_type_names(workflow)
    result: dict[str, str] = {}
    for port, decl in inputs.items():
        type_name = decl.get("type") if isinstance(decl, dict) else None
        if type_name in objects:
            result[port] = type_name
    return result


def inject_boundary_ids(
    boundary: dict | None, workflow: dict, id_gen: IdGenerator
) -> dict | None:
    """Return `boundary` with each Object input's view carrying an ``_id``.

    For every Object-bearing entry input, ensure ``boundary.inputs[port].view`` exists,
    fill any declared view field the user omitted with a typed default, and mint ``_id``
    (keyed by the port) unless one is already present -- so a result boundary fed back in
    round-trips its ids. `workflow` must be the ``_id``-injected document (so the view
    schema includes ``_id``). Returns `boundary` unchanged when it is None or has no
    Object inputs. Mutates a deep copy, not the caller's dict."""
    obj_inputs = entry_object_inputs(workflow)
    if boundary is None or not obj_inputs:
        return boundary
    out = copy.deepcopy(boundary)
    inputs = out.setdefault("boundary", {}).setdefault("inputs", {})
    for port, type_name in obj_inputs.items():
        schema = _view_schema(workflow, type_name)
        desc = inputs.setdefault(port, {})
        view = desc.get("view")
        if not isinstance(view, dict):
            view = {}
        # Fill declared non-`_id` fields the user omitted with typed defaults, so the
        # seeded view conforms (closed-shape: exactly the declared fields).
        for field, descriptor in schema.items():
            if field != RESERVED_ID and field not in view:
                view[field] = _default_field(descriptor)
        if not view.get(RESERVED_ID):
            view[RESERVED_ID] = id_gen.new_id(f"boundary:{port}")
        desc["view"] = view
    return out


def _object_output_ports(definition: dict | None) -> tuple[list[str], dict[str, str]]:
    """From a process definition's ``objects`` section, return ``(created, mapped)``:
    ``created`` = object output ports listed under ``objects.create``; ``mapped`` =
    ``{output_port: input_port}`` from ``objects.map`` (identity carried through)."""
    objects = ((definition or {}).get("objects")) or {}
    create = objects.get("create") or []
    created = [ref.split(".", 1)[1] for ref in create if isinstance(ref, str) and "." in ref]
    mapped: dict[str, str] = {}
    for out_ref, in_ref in (objects.get("map") or {}).items():
        if _is_ref(out_ref) and _is_ref(in_ref):
            mapped[out_ref.split(".", 1)[1]] = in_ref.split(".", 1)[1]
    return created, mapped


def _is_ref(ref: object) -> bool:
    """A namespaced objects path like ``outputs.plate`` / ``inputs.plate``."""
    return isinstance(ref, str) and "." in ref


def _declares_id(output_schema: dict | None, port: str) -> bool:
    """Whether output `port`'s value-shape descriptor is a record declaring ``_id`` --
    i.e. the port's Object type had ``_id`` injected (`inject_id_field`). This gates all
    stamping: on a workflow that was NOT rewritten (so its Object views have no ``_id``),
    every port fails this test and stamping is a no-op, keeping non-labcode use correct."""
    desc = (output_schema or {}).get(port)
    return (
        isinstance(desc, dict)
        and desc.get("kind") == "record"
        and RESERVED_ID in (desc.get("fields") or {})
    )


def stamp_object_ids(
    outputs: dict,
    definition: dict | None,
    inputs: dict,
    node,
    id_gen: IdGenerator,
    output_schema: dict | None = None,
) -> dict:
    """Stamp ``_id`` onto a process op's produced Object output views (mutates `outputs`).

    Only ports whose value-shape declares ``_id`` (`_declares_id`) are touched, so on a
    workflow labcode did not rewrite this is a no-op.

    * A **mapped** Object output (``objects.map`` ``outputs.P: inputs.Q``) carries the
      ``_id`` of its input ``Q`` -- identity is preserved even if the device script
      returned the port explicitly (overwriting the carried view).
    * A **created** Object output (``objects.create``) whose ``_id`` is empty/absent gets
      a freshly minted id, keyed by this node instance + port -- so two creates of the
      same process (different nodes) get distinct, reproducible ids.

    `node` is the workflow provenance (a node-path tuple, or None); `inputs` are the op's
    input views. Non-dict output values are left untouched (a conformance error, caught
    downstream)."""
    created, mapped = _object_output_ports(definition)
    node_key = "/".join(node) if node else "?"
    for port, src in mapped.items():
        if not _declares_id(output_schema, port):
            continue
        view = outputs.get(port)
        src_view = inputs.get(src)
        if isinstance(view, dict) and isinstance(src_view, dict) and RESERVED_ID in src_view:
            view[RESERVED_ID] = src_view[RESERVED_ID]
    for port in created:
        if not _declares_id(output_schema, port):
            continue
        view = outputs.get(port)
        if isinstance(view, dict) and not view.get(RESERVED_ID):
            view[RESERVED_ID] = id_gen.new_id(f"node:{node_key}:{port}")
    return outputs
