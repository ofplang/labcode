"""Behavioral tests for the labcode dispatcher.

These exercise only `lc`-level routing; each subcommand's behavior is covered in
its own ofplang sibling repository. `validate` is used as the dispatch probe
because it is the lightest sibling (PyYAML only, no ortools/numpy).
"""

from __future__ import annotations

import pytest

from labcode.cli import main


def test_no_args_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: lc" in capsys.readouterr().err


def test_help_goes_to_stdout_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: lc" in out
    assert "validate" in out and "schedule" in out and "run" in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("lc ")


def test_unknown_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_unrecognized_option_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--nope"]) == 2
    assert "unrecognized option" in capsys.readouterr().err


def test_dispatches_to_subcommand() -> None:
    # A missing path is the validator's own usage error (exit 2), returned as a
    # plain int; proves routing reaches the sibling and its exit code passes
    # through unchanged. (`validate --help` would instead raise SystemExit from
    # argparse, which is correct at runtime but awkward to assert on here.)
    assert main(["validate", "no-such-file.yaml"]) == 2
