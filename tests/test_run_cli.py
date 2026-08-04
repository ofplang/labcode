"""Tests for `lc run` (`labcode.run_cli`): the dialect front door and end-to-end
execution of an environment `x-labcode.script` on the labcode backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from labcode import run_cli
from labcode.backend import labcode_backend_factory

FIX = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parent.parent / "examples"
WF = str(FIX / "device_script.workflow.yaml")
ENV = str(FIX / "device_script.env.yaml")
TWF = str(FIX / "transport.workflow.yaml")
TENV = str(FIX / "transport.env.yaml")

NO_SCRIPT_ENV = """\
time: {unit: second}
devices: [{id: rack, spots: [slot]}]
transporters: [{id: arm}]
transports: []
processes:
  measure:
    modes:
      - {id: v0, duration: 3}
objective: {kind: makespan}
"""


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_e2e_env_script_really_computes_output():
    # Drive the workflow on the labcode backend: the env x-labcode.script (return
    # {"od": 0.42}) runs out-of-process and its value flows to the workflow output.
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from ofplang.run.runner import RollingRunner

    clock = FakeClock()
    factory = labcode_backend_factory(
        seconds_per_tick=0.001, monotonic=clock.monotonic, sleep=clock.sleep
    )
    runner = RollingRunner(WF, ENV, backend_factory=factory, random_seed=0)
    runner.run()
    assert runner.outputs == {"od": 0.42}


def test_e2e_lc_run_stamps_deterministic_object_ids(tmp_path):
    # End to end through `lc run`: the Tube boundary input and the created Plate get a
    # reserved `_id`, the Tube's id round-trips to the output (identity preserved), and
    # two runs produce identical ids (reproducible, provenance-keyed).
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    import yaml

    base = [
        str(EXAMPLES / "plate_line.workflow.yaml"),
        "--env", str(EXAMPLES / "plate_line.env.yaml"),
        "--boundary", str(EXAMPLES / "plate_line.boundary.yaml"),
        "--seconds-per-tick", "0.001",
        "-o", str(tmp_path / "status.yaml"),
    ]
    rb1, rb2 = tmp_path / "rb1.yaml", tmp_path / "rb2.yaml"
    assert run_cli.main([*base, "--boundary-out", str(rb1)]) == 0
    assert run_cli.main([*base, "--boundary-out", str(rb2)]) == 0

    doc1 = yaml.safe_load(rb1.read_text(encoding="utf-8"))
    out_tube = doc1["boundary"]["outputs"]["tube"]["view"]
    in_tube = doc1["boundary"]["inputs"]["tube"]["view"]
    assert out_tube["_id"] and out_tube["_id"] == in_tube["_id"]  # minted + identity carried
    assert doc1 == yaml.safe_load(rb2.read_text(encoding="utf-8"))  # deterministic


def test_e2e_lc_run_expands_imported_object_types_and_stamps_ids(tmp_path):
    # `$import` and `_id` compose: the object types (Plate/Tube) are moved into an
    # imported fragment, so the shared front door must expand it first; then
    # LabcodeRunner injects `_id` into those *imported* object types. The Tube id
    # still round-trips, proving expansion happens before `_id` injection.
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    import yaml

    src = (EXAMPLES / "plate_line.workflow.yaml").read_text(encoding="utf-8")
    head, body = src[: src.index("types:")], src[src.index("processes:") :]
    (tmp_path / "plate_types.yaml").write_text(
        "Plate:\n  domain: object\n  view:\n    barcode: { type: String }\n"
        "Tube:\n  domain: object\n",
        encoding="utf-8",
    )
    wf = tmp_path / "plate_line.workflow.yaml"
    wf.write_text(head + "types:\n  $import: ./plate_types.yaml\n" + body, encoding="utf-8")

    rb = tmp_path / "rb.yaml"
    code = run_cli.main(
        [
            str(wf),
            "--env", str(EXAMPLES / "plate_line.env.yaml"),
            "--boundary", str(EXAMPLES / "plate_line.boundary.yaml"),
            "--seconds-per-tick", "0.001",
            "-o", str(tmp_path / "status.yaml"),
            "--boundary-out", str(rb),
        ]
    )
    assert code == 0
    doc = yaml.safe_load(rb.read_text(encoding="utf-8"))
    out_tube = doc["boundary"]["outputs"]["tube"]["view"]
    in_tube = doc["boundary"]["inputs"]["tube"]["view"]
    assert out_tube["_id"] and out_tube["_id"] == in_tube["_id"]


def test_cli_dialect_error_is_a_usage_error(tmp_path, capsys):
    # A non-python x-labcode script is rejected at the dialect front door (exit 2), before
    # any execution.
    bad_env = tmp_path / "env.yaml"
    bad_env.write_text(
        Path(ENV).read_text(encoding="utf-8").replace("language: python", "language: r"),
        encoding="utf-8",
    )
    code = run_cli.main([WF, "--env", str(bad_env)])
    assert code == 2
    assert "x-labcode error" in capsys.readouterr().err


def test_e2e_transport_script_runs_and_run_completes():
    # A workflow with a transport whose route carries an x-labcode.script: the script runs
    # out-of-process when the Sample is moved, and the run completes. Driven by LabcodeRunner
    # (the canonical labcode entry), which injects `_id` into the Sample's view.
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    clock = FakeClock()
    # running_task_margin>=1 (what `lc run` defaults to): the real transport overruns its
    # duration estimate, so its successor must not be dispatched onto the still-busy device.
    runner = LabcodeRunner(
        TWF, TENV, seconds_per_tick=0.001, monotonic=clock.monotonic, sleep=clock.sleep,
        random_seed=0, running_task_margin=1,
    )
    status = runner.run()
    assert not runner.failed
    assert all(a["status"] == "completed" for a in status["activities"])


def test_e2e_failing_transport_script_fails_the_run(tmp_path):
    # A transport script that raises really ran (out-of-process) and its failure propagates:
    # the move ends failed and the run stops. Proves the transport child + failure path.
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    env = tmp_path / "env.yaml"
    env.write_text(
        Path(TENV).read_text(encoding="utf-8").replace(
            "moved = (view, from_spot, to_spot, transporter)",
            "raise RuntimeError('gripper stuck')",
        ),
        encoding="utf-8",
    )
    clock = FakeClock()
    runner = LabcodeRunner(
        TWF, str(env), seconds_per_tick=0.001, monotonic=clock.monotonic, sleep=clock.sleep,
        random_seed=0, running_task_margin=1,
    )
    runner.run()
    assert runner.failed
    assert runner.failure is not None
    assert "gripper stuck" in (runner.failure.detail or "")


def test_e2e_partial_read_carries_the_plate(tmp_path):
    # The plate_line example, with `read` returning {} (fully partial): the Plate is still
    # carried through (objects.map) so the line completes, and od takes its typed default.
    # Exercises the labcode child (raw partial) + _resolve_model merge end to end.
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    from labcode.runner import LabcodeRunner

    env = tmp_path / "env.yaml"
    env.write_text(
        (EXAMPLES / "plate_line.env.yaml").read_text(encoding="utf-8").replace(
            'return {"od": 0.42}', "return {}"
        ),
        encoding="utf-8",
    )
    clock = FakeClock()
    # plate_line takes a Tube in at the run boundary (on the rack) and returns it; the
    # Plate is created internally. LabcodeRunner injects `_id` and mints the boundary id.
    boundary = {
        "boundary": {
            "inputs": {"tube": {"spot": "rack.slot"}},
            "outputs": {"od": {}, "tube": {"spot": "rack.slot"}},
        }
    }
    runner = LabcodeRunner(
        str(EXAMPLES / "plate_line.workflow.yaml"), str(env), boundary,
        seconds_per_tick=0.001, monotonic=clock.monotonic, sleep=clock.sleep,
        random_seed=0, running_task_margin=1,
    )
    status = runner.run()
    assert not runner.failed
    assert all(a["status"] == "completed" for a in status["activities"])  # plate reached store
    assert runner.outputs["od"] == 0.0  # od defaulted (read returned {})
    assert "tube" in runner.outputs  # the Tube is carried back out


def _unreachable_env(tmp_path) -> str:
    """`reroute_device.env.yaml` with both destinations pointed at a closed port.

    Port 9 (discard) is not served on a development machine, so this is what an operator
    sees when the lab is not running: the probe reaches neither station, and `target` --
    which can only run on one of them -- has nowhere left to go."""
    source = (FIX / "reroute_device.env.yaml").read_text(encoding="utf-8")
    env = tmp_path / "unreachable.env.yaml"
    env.write_text(
        source.replace("port: 50101", "port: 9").replace("port: 50102", "port: 9"),
        encoding="utf-8",
    )
    return str(env)


def test_cli_reports_an_unreachable_machine_and_says_so_when_the_run_stops(tmp_path, capsys):
    # `lc run` is where an operator hears about availability: each machine is named when its
    # reachability changes, and the failure repeats which ones were unreachable -- the
    # scheduler's "no route" message alone would not say why there is no route.
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    code = run_cli.main([TWF, "--env", _unreachable_env(tmp_path), "--seconds-per-tick", "0.001"])
    assert code == 1
    err = capsys.readouterr().err
    assert "'station_1' is unreachable (probe)" in err
    assert "'station_2' is unreachable (probe)" in err
    assert "unreachable at the probe: station_1, station_2" in err


def test_cli_no_probe_ignores_the_policy(tmp_path, capsys):
    # The same environment runs when probing is turned off: every machine is assumed
    # reachable, which is what the flag is for when the lab is elsewhere.
    pytest.importorskip("ofplang.schedule", reason="ofplang-schedule not installed")
    code = run_cli.main(
        [TWF, "--env", _unreachable_env(tmp_path), "--seconds-per-tick", "0.001", "--no-probe"]
    )
    assert code == 0
    assert "unreachable" not in capsys.readouterr().err


def test_cli_warns_on_typed_default(tmp_path, capsys, monkeypatch):
    # A process with no script warns (typed-default no-op) but still runs. Stub the run so
    # the test does not spend real wall-clock time.
    from ofplang.run.app import RunResult

    env = tmp_path / "env.yaml"
    env.write_text(NO_SCRIPT_ENV, encoding="utf-8")
    monkeypatch.setattr(
        run_cli, "run_labcode",
        lambda *a, **k: RunResult(status={"now": 0, "activities": []}, result_boundary={},
                                  failed=False, failure=None),
    )
    out = tmp_path / "status.yaml"
    code = run_cli.main([WF, "--env", str(env), "-o", str(out)])
    assert code == 0
    assert "typed-default no-op" in capsys.readouterr().err
