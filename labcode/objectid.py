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
import re

from ofplang.run.simulator import DeviceComputationError

from labcode.idgen import IdGenerator

# The reserved view field name. Not user-declarable (see `reserved_collisions`).
RESERVED_ID = "_id"

# Typed defaults per v0 primitive, to fill a boundary Object input's non-`_id` view
# fields the user omitted (a view value must carry exactly its declared fields).
_PRIMITIVE_DEFAULTS = {"Bool": False, "Int": 0, "Float": 0.0, "String": ""}

# A unit suffix on a numeric primitive (v0 §28.2), which a view field type may
# carry (§28.9). A unit has no runtime representation, so a default is the
# default of the base type: `Float[uL]` defaults to 0.0, like `Float`.
_UNIT_SUFFIX = re.compile(r"(Int|Float)\[[^\[\]]*\]")


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
    yields its default; an Array yields ``[]``; anything else falls back to ``None``.

    An Array view field is written ``Array<T>`` (v0 §2.5, §7.4) -- the bare name
    ``Array`` is not a v0 type expression -- so the test is on the constructor,
    which also covers the ``Array< T >`` spelling v0 permits."""
    type_name = descriptor.get("type") if isinstance(descriptor, dict) else None
    if isinstance(type_name, str):
        type_name = _UNIT_SUFFIX.sub(r"\1", type_name)
    if type_name in _PRIMITIVE_DEFAULTS:
        return _PRIMITIVE_DEFAULTS[type_name]
    if isinstance(type_name, str) and type_name.startswith("Array<"):
        return []
    return None


def _object_element(type_expr: object, objects: set[str]) -> tuple[str, int] | None:
    """`(object type name, Array depth)` when `type_expr` is an Object type or an Array
    of one (nested to any depth) -- `("Plate", 0)` for `Plate`, `("Plate", 1)` for
    `Array<Plate>` -- else None."""
    if not isinstance(type_expr, str):
        return None
    expr, depth = type_expr.strip(), 0
    while expr.startswith("Array<") and expr.endswith(">"):
        expr, depth = expr[len("Array<"):-1].strip(), depth + 1
    return (expr, depth) if expr in objects else None


def entry_object_inputs(workflow: dict) -> dict[str, tuple[str, int]]:
    """Map each Object-bearing entry (whole-workflow) input port -> `(type name, depth)`.

    The entry composite's inputs whose declared type is a ``domain: object`` type, or an
    Array of one, are the run-boundary Objects; a boundary must place each on a spot (an
    Array: one spot per element) and, with this feature, each gets an ``_id``. `depth`
    is how deeply the port nests Arrays, 0 for a single Object."""
    entry = workflow.get("entry")
    processes = workflow.get("processes") or {}
    proc = processes.get(entry) or {}
    inputs = proc.get("inputs") or {}
    objects = object_type_names(workflow)
    result: dict[str, tuple[str, int]] = {}
    for port, decl in inputs.items():
        found = _object_element(decl.get("type") if isinstance(decl, dict) else None, objects)
        if found is not None:
            result[port] = found
    return result


def _key(job: str | None, rest: str) -> str:
    """A mint key, prefixed by the job where there is one (SPEC §6.11).

    🔴 **Only where there is one.** Two jobs of one workflow bind the same port names
    and render the same node paths, so without the job a reproducible generator gives
    one job's plate the other's identity -- silently, and the same way every run. But a
    run of a *single* workflow names no job, and prefixing there would change every id
    labcode has ever minted for one: the checked-in example observations are that
    invariant, and a run that was reproducible must stay reproducible in the same way.
    """
    return f"job:{job}:{rest}" if job else rest


def inject_boundary_ids(
    boundary: dict | None,
    workflow: dict,
    id_gen: IdGenerator,
    job: str | None = None,
    warnings: list | None = None,
) -> dict | None:
    """Return `boundary` with each Object input's view carrying an ``_id``.

    For every Object-bearing entry input -- each element, for an Array of Objects --
    ensure its view exists, fill any declared view field the user omitted with a typed
    default, and mint ``_id`` (keyed by the port, ``plates[1]`` for an element, and by
    `job` where the run names one) unless one is already present -- so a result boundary
    fed back in round-trips its ids. `workflow` must be the ``_id``-injected document
    (so the view schema includes ``_id``). Returns `boundary` unchanged when it is None
    or has no Object inputs. Mutates a deep copy, not the caller's dict.

    A view may be written in part (D59 E): the fields it leaves out take their type's
    default. That is a value the run makes up, so each element that needed one is
    reported -- an `entry_input_defaulted` `RunWarning` appended to `warnings`, naming
    the fields. ``_id`` is never among them: minting it is this feature's identity, not
    a default. A view that is not a mapping (an Array's: not a list of mappings, in the
    shape of its spots) is refused rather than replaced."""
    obj_inputs = entry_object_inputs(workflow)
    if boundary is None or not obj_inputs:
        return boundary
    out = copy.deepcopy(boundary)
    inputs = out.setdefault("boundary", {}).setdefault("inputs", {})
    for port, (type_name, depth) in obj_inputs.items():
        desc = inputs.setdefault(port, {})
        views = _BoundaryViews(
            port, depth, _view_schema(workflow, type_name), "view" in desc,
            id_gen, job, warnings,
        )
        desc["view"] = views.walk(desc.get("view"), desc.get("spot"), (), 0)
    return out


class _BoundaryViews:
    """Completes one boundary port's view(s): `walk` follows its spot binding as deep as
    the port nests Arrays, and `complete` fills, reports and mints one Object's view."""

    def __init__(self, port, depth, schema, supplied, id_gen, job, warnings) -> None:
        self.port, self.depth, self.schema, self.supplied = port, depth, schema, supplied
        self.id_gen, self.job, self.warnings = id_gen, job, warnings

    def walk(self, view, spots, index: tuple, level: int):
        """One view per spot: the spots say how many Objects there are, so an omitted
        view is that many empty ones."""
        from ofplang.run.runner.runner import RunnerError
        from ofplang.schedule.core.identifiers import format_element

        if level == self.depth:
            return self.complete(view, index)
        items = spots if isinstance(spots, list) else []
        if view is None:
            view = [None] * len(items)
        elif not isinstance(view, list):
            raise RunnerError(
                f"boundary input {format_element(self.port, index)!r}: an Array of Objects "
                f"takes a list of views, one per spot, not {type(view).__name__}"
            )
        return [
            self.walk(item, items[i] if i < len(items) else None, (*index, i), level + 1)
            for i, item in enumerate(view)
        ]

    def complete(self, view, index: tuple) -> dict:
        from ofplang.run.runner.job import RunWarning
        from ofplang.run.runner.runner import RunnerError
        from ofplang.schedule.core.identifiers import format_element

        label = format_element(self.port, index)
        if view is None:
            view = {}
        elif not isinstance(view, dict):
            raise RunnerError(
                f"boundary input {label!r}: its view must be a mapping of view fields, "
                f"not {type(view).__name__}"
            )
        # Fill declared non-`_id` fields the user omitted with typed defaults, so the
        # seeded view conforms (closed-shape: exactly the declared fields) -- and say so.
        filled = [f for f in self.schema if f != RESERVED_ID and f not in view]
        for field in filled:
            view[field] = _default_field(self.schema[field])
        if filled and self.warnings is not None:
            written = "was written without" if self.supplied else "was not supplied, so has"
            self.warnings.append(RunWarning(
                "entry_input_defaulted",
                f"entry input {label!r} {written} view field(s) {filled}; "
                f"they run on their type's default",
                self.job or "",
            ))
        if not view.get(RESERVED_ID):
            view[RESERVED_ID] = self.id_gen.new_id(_key(self.job, f"boundary:{label}"))
        return view


def object_output_ports(definition: dict | None) -> tuple[list[str], dict[str, str]]:
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
    i.e. the port's Object type had ``_id`` injected (`inject_id_field`)."""
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
    job: str | None = None,
) -> dict:
    """Stamp ``_id`` onto a process op's produced Object output views (mutates `outputs`).

    Every Object output port MUST declare ``_id`` in its value-shape -- i.e. its type was
    ``_id``-injected (`inject_id_field`), which `LabcodeRunner` does for every labcode
    run. A port that does not is an error (`DeviceComputationError`): labcode's Object
    identity is an invariant, and a missing ``_id`` means an Object type escaped the
    rewrite (e.g. a bare `labcode_backend_factory` used without `LabcodeRunner`, or a
    `$import`-ed type). Surfacing it beats silently producing an id-less Object.

    * A **mapped** Object output (``objects.map`` ``outputs.P: inputs.Q``) carries the
      ``_id`` of its input ``Q`` -- identity is preserved even if the device script
      returned the port explicitly (overwriting the carried view).
    * A **created** Object output (``objects.create``) whose ``_id`` is empty/absent gets
      a freshly minted id, keyed by this node instance + port -- so two creates of the
      same process (different nodes) get distinct, reproducible ids -- **and by the job
      where the run names one**, since two jobs of one workflow render the same node
      path and would otherwise mint one identity for two plates.

    `node` is the workflow provenance (a node-path tuple, or None) and `job` which job of
    a joint run this is (or None); `inputs` are the op's input views."""
    created, mapped = object_output_ports(definition)
    for port in (*mapped, *created):
        if not _declares_id(output_schema, port):
            raise DeviceComputationError(
                f"Object output {port!r} has no {RESERVED_ID!r} in its view schema; every "
                f"labcode Object type must be _id-injected -- run via LabcodeRunner",
                code="missing_object_id",
            )
    # An iteration index is an int in the path (`Read/1`): rendered as its number, so a
    # path without one keys exactly as it always has.
    node_key = "/".join(str(step) for step in node) if node else "?"
    for port, src in mapped.items():
        view = outputs.get(port)
        src_view = inputs.get(src)
        # Every Object entering an op carries an `_id` -- minted at the boundary or at
        # its create, and carried since -- so an input without one is a broken
        # invariant. Said here rather than letting the output keep whatever `_id` it
        # had (a created default's `""`): the identity would be lost without a word.
        if not (isinstance(src_view, dict) and src_view.get(RESERVED_ID)):
            raise DeviceComputationError(
                f"mapped Object output {port!r} cannot carry an identity: its input "
                f"{src!r} has no {RESERVED_ID!r}",
                code="missing_object_id",
            )
        if isinstance(view, dict):
            view[RESERVED_ID] = src_view[RESERVED_ID]
    for port in created:
        view = outputs.get(port)
        if isinstance(view, dict) and not view.get(RESERVED_ID):
            view[RESERVED_ID] = id_gen.new_id(_key(job, f"node:{node_key}:{port}"))
    return outputs
