"""Tests for labcode's implicit Object identity (`labcode.objectid` + `labcode.idgen`):
the reserved ``_id`` view key -- type rewrite, boundary minting, create/map stamping,
reproducible id generation, and the reserved-collision rejection.
"""

from __future__ import annotations

import pytest
from ofplang.run.simulator import DeviceComputationError

from labcode.idgen import RealUuid4Generator, SeededUuid4Generator
from labcode.objectid import (
    RESERVED_ID,
    inject_boundary_ids,
    inject_id_field,
    reserved_collisions,
    stamp_object_ids,
)

# A tiny workflow: a Plate (view: barcode) created by `load` and carried by `read`, plus
# a view-less Tube that enters at the run boundary.
WORKFLOW = {
    "types": {
        "Plate": {"domain": "object", "view": {"barcode": {"type": "String"}}},
        "Tube": {"domain": "object"},
        "Count": {"domain": "data", "view": {"n": {"type": "Int"}}},
    },
    "processes": {
        "load": {"kind": "atomic", "outputs": {"plate": {"type": "Plate"}},
                 "objects": {"create": ["outputs.plate"]}},
        "main": {"kind": "composite", "inputs": {"tube": {"type": "Tube"}},
                 "outputs": {"tube": {"type": "Tube"}}},
    },
    "entry": "main",
}

# The value-shape descriptor an `_id`-injected Plate output carries (as the runner builds
# it from the rewritten type): a record whose fields include `_id`.
PLATE_SCHEMA = {
    "plate": {
        "kind": "record",
        "fields": {"barcode": {"kind": "primitive", "name": "String"},
                   RESERVED_ID: {"kind": "primitive", "name": "String"}},
    }
}
# The same port on a workflow that was NOT rewritten: no `_id` in the record.
PLATE_SCHEMA_NO_ID = {
    "plate": {"kind": "record", "fields": {"barcode": {"kind": "primitive", "name": "String"}}}
}


# -- type rewrite -------------------------------------------------------------

def test_inject_id_field_adds_id_to_object_views_only():
    out = inject_id_field(WORKFLOW)
    assert out["types"]["Plate"]["view"][RESERVED_ID] == {"type": "String"}
    # A view-less Object type gains a view carrying just `_id`.
    assert out["types"]["Tube"]["view"] == {RESERVED_ID: {"type": "String"}}
    # A non-Object (data) type is untouched.
    assert RESERVED_ID not in out["types"]["Count"]["view"]
    # The input document is not mutated (deep copy).
    assert "view" not in WORKFLOW["types"]["Tube"]


def test_reserved_collisions_flags_user_declared_id():
    clash = {"types": {"Plate": {"domain": "object", "view": {RESERVED_ID: {"type": "String"}}}}}
    assert reserved_collisions(clash) == ["Plate"]
    assert reserved_collisions(WORKFLOW) == []


# -- boundary minting ---------------------------------------------------------

def test_inject_boundary_ids_mints_for_object_input():
    rewritten = inject_id_field(WORKFLOW)
    boundary = {"boundary": {"inputs": {"tube": {"spot": "rack.slot"}}}}
    out = inject_boundary_ids(boundary, rewritten, SeededUuid4Generator(0))
    view = out["boundary"]["inputs"]["tube"]["view"]
    assert view[RESERVED_ID]  # minted
    assert out["boundary"]["inputs"]["tube"]["spot"] == "rack.slot"  # spot preserved


def test_inject_boundary_ids_keeps_an_existing_id_for_round_trip():
    rewritten = inject_id_field(WORKFLOW)
    boundary = {"boundary": {"inputs": {"tube": {"spot": "r.s", "view": {RESERVED_ID: "keep-me"}}}}}
    out = inject_boundary_ids(boundary, rewritten, SeededUuid4Generator(0))
    assert out["boundary"]["inputs"]["tube"]["view"][RESERVED_ID] == "keep-me"


# -- create / map stamping ----------------------------------------------------

def test_stamp_mints_created_object_id():
    outputs = {"plate": {"barcode": "P001"}}  # script's return, no _id
    definition = {"objects": {"create": ["outputs.plate"]}}
    stamp_object_ids(outputs, definition, {}, ("Load",), SeededUuid4Generator(0), PLATE_SCHEMA)
    assert outputs["plate"][RESERVED_ID]
    assert outputs["plate"]["barcode"] == "P001"


def test_stamp_carries_mapped_object_id():
    outputs = {"plate": {"barcode": "P001"}}  # script returned the port, dropping _id
    inputs = {"plate": {"barcode": "P001", RESERVED_ID: "abc"}}
    definition = {"objects": {"map": {"outputs.plate": "inputs.plate"}}}
    stamp_object_ids(
        outputs, definition, inputs, ("Read",), SeededUuid4Generator(0), PLATE_SCHEMA
    )
    assert outputs["plate"][RESERVED_ID] == "abc"  # identity carried from the input


def test_stamp_raises_when_type_does_not_declare_id():
    # An Object output whose type was not `_id`-injected is an invariant violation --
    # labcode Object identity is mandatory (LabcodeRunner injects it), so stamping errors
    # rather than silently producing an id-less (or non-conformant) Object.
    outputs = {"plate": {"barcode": "P001"}}
    definition = {"objects": {"create": ["outputs.plate"]}}
    with pytest.raises(DeviceComputationError, match="_id"):
        stamp_object_ids(
            outputs, definition, {}, ("Load",), SeededUuid4Generator(0), PLATE_SCHEMA_NO_ID
        )


# -- id generators ------------------------------------------------------------

def test_seeded_generator_is_deterministic_and_provenance_keyed():
    a, b = SeededUuid4Generator(0), SeededUuid4Generator(0)
    # Same seed + same key -> same id (reproducible, order-independent).
    assert a.new_id("node:Load:plate") == b.new_id("node:Load:plate")
    # Different provenance -> different id.
    assert a.new_id("node:Load:plate") != a.new_id("node:Other:plate")
    # A different seed -> different id for the same key.
    assert SeededUuid4Generator(1).new_id("k") != SeededUuid4Generator(0).new_id("k")
    # Output is uuid4-shaped (version nibble is 4).
    assert a.new_id("k")[14] == "4"


def test_real_generator_ignores_key_and_varies():
    gen = RealUuid4Generator()
    assert gen.new_id("k") != gen.new_id("k")  # fresh each call


# -- unit-annotated view fields (v0 §28.9) ------------------------------------

def test_boundary_fills_a_unit_annotated_view_field_with_its_base_default():
    # A view field may carry a unit where its base type is numeric (v0 §28.9),
    # and a unit has no runtime representation (§28), so the typed default is
    # the default of the base type. Before this, `Float[uL]` fell through to
    # None and the seeded boundary value did not conform.
    workflow = {
        "types": {
            "Vial": {
                "domain": "object",
                "view": {
                    "capacity": {"type": "Float[uL]"},
                    "slots": {"type": "Int[count_]"},
                    "label": {"type": "String"},
                },
            }
        },
        "processes": {
            "main": {"kind": "composite", "inputs": {"vial": {"type": "Vial"}}, "outputs": {}}
        },
        "entry": "main",
    }
    boundary = {"boundary": {"inputs": {"vial": {"spot": "bench.1"}}}}
    out = inject_boundary_ids(boundary, inject_id_field(workflow), SeededUuid4Generator(0))
    view = out["boundary"]["inputs"]["vial"]["view"]
    assert view["capacity"] == 0.0
    assert view["slots"] == 0
    assert view["label"] == ""
    assert isinstance(view[RESERVED_ID], str)


def test_boundary_fills_an_array_view_field_with_an_empty_list():
    # An Array view field is written `Array<T>` (v0 §2.5): the bare name `Array`
    # is not a v0 type expression, so the old exact-match test never fired and
    # the field was seeded with None -- which the runner's view conformance
    # rejects, since an Array value must be a list.
    workflow = {
        "types": {
            "Rack": {
                "domain": "object",
                "view": {
                    "wells": {"type": "Array<Int>"},
                    "nested": {"type": "Array<Array<Bool>>"},
                    "spaced": {"type": "Array< String >"},
                    "volumes": {"type": "Array<Float[uL]>"},
                },
            }
        },
        "processes": {
            "main": {"kind": "composite", "inputs": {"rack": {"type": "Rack"}}, "outputs": {}}
        },
        "entry": "main",
    }
    boundary = {"boundary": {"inputs": {"rack": {"spot": "bench.1"}}}}
    out = inject_boundary_ids(boundary, inject_id_field(workflow), SeededUuid4Generator(0))
    view = out["boundary"]["inputs"]["rack"]["view"]
    assert view["wells"] == []
    assert view["nested"] == []
    assert view["spaced"] == []
    assert view["volumes"] == []
