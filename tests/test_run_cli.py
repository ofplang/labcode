"""Tests for `lc run` (`labcode.run_cli`): the dialect front door and end-to-end
execution of an environment `x-labcode.script` on the labcode backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from labcode import run_cli
from labcode.backend import labcode_backend_factory

FIX = Path(__file__).parent / "fixtures"
WF = str(FIX / "device_script.workflow.yaml")
ENV = str(FIX / "device_script.env.yaml")

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


def test_cli_warns_on_typed_default(tmp_path, capsys, monkeypatch):
    # A process with no script warns (typed-default no-op) but still runs. Stub the run so
    # the test does not spend real wall-clock time.
    from ofplang.run.app import RunResult

    env = tmp_path / "env.yaml"
    env.write_text(NO_SCRIPT_ENV, encoding="utf-8")
    monkeypatch.setattr(
        run_cli, "run_workflow",
        lambda *a, **k: RunResult(status={"now": 0, "activities": []}, result_boundary={},
                                  failed=False, failure=None),
    )
    out = tmp_path / "status.yaml"
    code = run_cli.main([WF, "--env", str(env), "-o", str(out)])
    assert code == 0
    assert "typed-default no-op" in capsys.readouterr().err
