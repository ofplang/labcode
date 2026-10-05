"""A `map` / `fold` over an Array of plates, run on the labcode backend (D59 R3).

ofplang-schedule expands the structured nodes into their invocations and ofplang-run
reads the same expanded graph; what labcode adds is identity. Each plate of an Array
of Objects at the boundary is its own Object, so each gets its own `_id` -- keyed by
the element, `boundary:plates[i]` -- and an invocation's node path carries its
iteration index, which the `_id` of anything it creates is keyed by too.

The workflow is ofplang-schedule's `dispense_read` example, unchanged. Its `read`
declares a Pure Data output (`od`), so it needs a script; `dispense` has only Object
outputs and runs as a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")

from labcode.idgen import SeededUuid4Generator  # noqa: E402
from labcode.runner import LabcodeRunner  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
SPOTS = ["hotel.a", "hotel.b", "hotel.c"]

ENV = """\
time: {unit: second}
devices:
  - {id: hotel, spots: [a, b, c]}
  - {id: shelf, spots: [r]}
  - {id: dispenser, spots: [stage, reagent]}
  - {id: reader, spots: [stage]}
  - {id: rack, spots: [a, b, c]}
transporters: [{id: arm}]
transports:
  - {transporter: arm, from: hotel.a, to: dispenser.stage, duration: 3}
  - {transporter: arm, from: hotel.b, to: dispenser.stage, duration: 3}
  - {transporter: arm, from: hotel.c, to: dispenser.stage, duration: 3}
  - {transporter: arm, from: shelf.r, to: dispenser.reagent, duration: 3}
  - {transporter: arm, from: dispenser.reagent, to: shelf.r, duration: 3}
  - {transporter: arm, from: dispenser.stage, to: reader.stage, duration: 2}
  - {transporter: arm, from: reader.stage, to: rack.a, duration: 2}
  - {transporter: arm, from: reader.stage, to: rack.b, duration: 2}
  - {transporter: arm, from: reader.stage, to: rack.c, duration: 2}
processes:
  dispense:
    modes:
      - devices: [dispenser]
        duration: 5
        input_spots: {plate: dispenser.stage, reagent: dispenser.reagent}
        output_spots: {plate: dispenser.stage, reagent: dispenser.reagent}
  read:
    modes:
      - devices: [reader]
        duration: 10
        input_spots: {plate: reader.stage}
        output_spots: {plate: reader.stage}
        x-labcode:
          script:
            language: python
            code: |
              return {"od": 0.5}
"""


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _workflow() -> dict:
    doc = yaml.safe_load((FIX / "dispense_read.workflow.yaml").read_text(encoding="utf-8"))
    doc["types"]["Plate"]["view"] = {"barcode": {"type": "String"}}
    return doc


def _run(tmp_path, views):
    env = tmp_path / "env.yaml"
    env.write_text(ENV, encoding="utf-8")
    plates: dict = {"spot": SPOTS}
    if views is not None:
        plates["view"] = views
    boundary = {"boundary": {
        "inputs": {"reagent": {"spot": "shelf.r"}, "plates": plates, "volume": {"view": 2.5}},
        "outputs": {"reagent": {"spot": "shelf.r"},
                    "plates": {"spot": ["rack.a", "rack.b", "rack.c"]}},
    }}
    clock = FakeClock()
    runner = LabcodeRunner(
        _workflow(), str(env), boundary, seconds_per_tick=0.001,
        monotonic=clock.monotonic, sleep=clock.sleep, random_seed=0, running_task_margin=1,
        id_generator=SeededUuid4Generator(seed=7), observe=True,
    )
    try:
        runner.run()
    finally:
        runner.sim.close()
    return runner


def test_each_plate_keeps_its_own_identity_through_the_fold_and_the_map(tmp_path):
    runner = _run(tmp_path, [{"barcode": "A"}, {"barcode": "B"}, {"barcode": "C"}])
    assert not runner.failed, runner.failure
    plates = runner.outputs["plates"]
    assert [p["barcode"] for p in plates] == ["A", "B", "C"]
    ids = SeededUuid4Generator(seed=7)
    # Keyed by the element, in element order; carried through dispense and read.
    assert [p["_id"] for p in plates] == [ids.new_id(f"boundary:plates[{i}]") for i in range(3)]
    assert runner.outputs["ods"] == [0.5, 0.5, 0.5]
    reads = {
        tuple(e["node"]): e["inputs"]["plate"]["view"]["_id"]
        for e in runner.observations
        if e.get("kind") == "processing" and e["node"][0] == "Read"
    }
    assert reads == {("Read", i): plates[i]["_id"] for i in range(3)}
    # Every view was supplied in full, so nothing was made up -- `_id` is not a default.
    assert runner.warnings == []


def test_views_left_out_are_said_once_per_plate(tmp_path):
    runner = _run(tmp_path, None)
    assert not runner.failed, runner.failure
    defaulted = [w for w in runner.warnings if w.code == "entry_input_defaulted"]
    assert sorted(w.message.split("'")[1] for w in defaulted) == [
        "plates[0]", "plates[1]", "plates[2]"
    ]
