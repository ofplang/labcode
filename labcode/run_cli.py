"""``lc run``: drive a workflow on the labcode execution backend.

Mirrors ``ofp-run run`` (rolling-horizon), but (a) injects the labcode backend --
`SubprocessBackend` sourcing each device op's script from the environment's
``x-labcode`` extension, run out-of-process on a wall clock -- and (b) adds the labcode
dialect front door: after the shared workflow front door (`ofplang.run.front_door_check`),
it validates the env ``x-labcode`` extension (P5, `labcode.dialect`) and warns about ops
that will run as a typed-default no-op.

It also owns what the operator hears about **availability**: the environment may ask for
its machines to be checked (``x-labcode.probe``), and this prints each machine whose
reachability changes, so a run that re-routes -- or stops because it cannot -- says why.

This module is the ``run`` entry of the ``lc`` dispatcher (see `labcode.cli`); it owns
argument parsing, I/O, and exit-code mapping, delegating the actual run to the shared
`ofplang.run.run_workflow` front door with the labcode `backend_factory`.

Exit codes (same as ``ofp-run``): 0 success, 1 execution failed, 2 usage / input error.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml
from ofplang.run import FrontDoorResult, front_door_check
from ofplang.run.runner import (
    DEFAULT_MAX_TICKS,
    ContractSyntaxError,
    RunnerError,
    load_document,
    serialize_document,
)
from ofplang.run.simulator import SimulatorError

from labcode.backend import DEFAULT_SECONDS_PER_TICK, FROM_ENVIRONMENT
from labcode.dialect import validate_dialect
from labcode.extension import DEFAULT_OP_TIMEOUT
from labcode.runner import run_labcode

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lc run",
        description="Run an ofplang v0 workflow on the labcode backend (env x-labcode scripts).",
    )
    p.add_argument("workflow", metavar="WORKFLOW", help="ofplang v0 workflow YAML")
    p.add_argument("--env", required=True, metavar="ENV", help="execution environment YAML (§5)")
    p.add_argument("--boundary", metavar="DOC", help="run boundary document (§6.8 / value layer)")
    p.add_argument("--seed", type=int, metavar="N", help="scheduler random seed")
    p.add_argument(
        "--margin", type=int, default=None, metavar="M",
        help="running-task margin for replans (default: the poll interval)",
    )
    p.add_argument(
        "--poll-interval", type=int, default=1, metavar="D",
        help="poll every D time units (default 1)",
    )
    p.add_argument(
        "--seconds-per-tick", type=float, default=DEFAULT_SECONDS_PER_TICK, metavar="S",
        help=f"real seconds per env time tick (default {DEFAULT_SECONDS_PER_TICK:g})",
    )
    p.add_argument(
        "--speed", type=float, default=1.0, metavar="X",
        help="wall-clock speed multiplier (2.0 = twice as fast)",
    )
    p.add_argument(
        "--max-ticks", type=int, default=DEFAULT_MAX_TICKS, metavar="N",
        help=(
            f"give up after N ticks as non-terminating, or 0 for no limit (default "
            f"{DEFAULT_MAX_TICKS}); one tick is --poll-interval x --seconds-per-tick of real "
            "time, so at the default cadence the default limit is weeks away -- a stuck "
            "instrument is caught by --op-timeout, not by this"
        ),
    )
    p.add_argument("-o", "--output", metavar="OUT", help="write the final status YAML here")
    p.add_argument("--boundary-out", metavar="FILE", help="write the result boundary document here")
    p.add_argument(
        # Same flag and meaning as `ofp-run run --observation-out`: the value layer's
        # companion to the status document, streamed one YAML document per completed
        # activity. Forwarded verbatim to `run_labcode`, which already took the argument.
        "--observation-out", metavar="FILE",
        help="stream the observation document here (YAML multi-document): each completed"
             " activity's input/output view values, appended as it finishes",
    )
    p.add_argument(
        "--no-validate", action="store_true",
        help="skip the ofplang-validate front-door check of the workflow",
    )
    p.add_argument(
        "--no-probe", action="store_true",
        help="ignore the environment's x-labcode.probe policies and treat every machine as"
             " reachable (the document is still validated)",
    )
    # One operation timeout for the whole lab, in real seconds. The two forms exclude each
    # other: asking for a limit and for no limit in the same breath is a mistake, not a
    # precedence puzzle to resolve quietly.
    timeout = p.add_mutually_exclusive_group()
    timeout.add_argument(
        "--op-timeout", type=float, default=None, metavar="S",
        help="stop and fail an operation that has run for S real seconds (default: the"
             f" environment's x-labcode.op_timeout, else {DEFAULT_OP_TIMEOUT:g})",
    )
    timeout.add_argument(
        "--no-op-timeout", action="store_true",
        help="let an operation run for as long as it takes, whatever the environment says",
    )
    return p


def _print_front_door(fd: FrontDoorResult) -> None:
    for diag in fd.diagnostics:
        if diag.file and diag.line:
            locator = f"{diag.file}:{diag.line}:{diag.col}"
        else:
            locator = diag.path or "<root>"
        detail = f"  {diag.path}" if diag.file and diag.path else ""
        message = f"  {diag.message}" if diag.message else ""
        print(f"{locator}: error {diag.code}{detail}{message}", file=sys.stderr)
    if fd.unsupported is not None:
        print(f"lc run: unsupported: {fd.unsupported}", file=sys.stderr)


def _read_document(path: str, what: str) -> tuple[dict | None, int | None]:
    try:
        doc = load_document(path)
    except (OSError, yaml.YAMLError) as exc:
        print(f"lc run: cannot read {what} {path!r}: {exc}", file=sys.stderr)
        return None, EXIT_USAGE
    if not isinstance(doc, dict):
        print(f"lc run: {what} must be a mapping: {path!r}", file=sys.stderr)
        return None, EXIT_USAGE
    return doc, None


def _write(text: str, output, what: str) -> int | None:
    """Write `text` to the `output` path (or stdout when unset), returning EXIT_USAGE
    if the file could not be written.

    Mirrors `ofplang.run.cli._write` (see L3/O1: this CLI and `ofp-run`'s are kept in
    step by hand). An unwritable output path is an input error like an unreadable one,
    but it is discovered once the run has already occupied real machines, so the caller
    reports it and still attempts the other outputs rather than dropping them.
    """
    if not output:
        sys.stdout.write(text)
        return None
    try:
        Path(output).write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"lc run: cannot write {what} {str(output)!r}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return None


def _emit(status: dict, output) -> int | None:
    return _write(serialize_document(status), output, "status")


def main(argv: Sequence[str] | None = None) -> int:
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    args = _build_parser().parse_args(argv)

    for label, path in (("workflow", args.workflow), ("environment", args.env)):
        if not Path(path).is_file():
            print(f"lc run: {label} not found: {path!r}", file=sys.stderr)
            return EXIT_USAGE

    # A negative tick count is not a limit at all (zero *is* how this one says "no
    # limit", as it does upstream), so it is a usage error rather than something to
    # interpret.
    if args.max_ticks < 0:
        print(
            f"lc run: --max-ticks must not be negative: {args.max_ticks}; use 0 for no limit",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # A limit of zero (or less) is not "no limit" -- that is what --no-op-timeout says --
    # so it is a usage error rather than something to interpret.
    if args.op_timeout is not None and not args.op_timeout > 0:
        print(
            f"lc run: --op-timeout must be a positive number of seconds "
            f"(got {args.op_timeout:g}); use --no-op-timeout for no limit",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Shared workflow front door (ofplang-validate + capability gate; validate skippable).
    fd = front_door_check(args.workflow, validate=not args.no_validate)
    if not fd.ok:
        _print_front_door(fd)
        return EXIT_USAGE

    # labcode dialect front door: validate the env x-labcode extension (P5) over the
    # workflow (for (1)/(2) exclusivity) and the environment. Warnings are printed but do
    # not block; errors are a usage error. Use the *expanded* workflow the shared front
    # door already resolved (`fd.document`), so `$import` is applied once, no second read
    # happens, and imported types/ops are visible to the dialect check and `_id` setup.
    workflow_doc = fd.document or {}
    env_doc, err = _read_document(args.env, "environment")
    if err is not None:
        return err
    dialect = validate_dialect(workflow_doc, env_doc or {})
    for warning in dialect.warnings:
        print(f"lc run: warning: {warning}", file=sys.stderr)
    if not dialect.ok:
        for error in dialect.errors:
            print(f"lc run: x-labcode error: {error}", file=sys.stderr)
        return EXIT_USAGE

    boundary = None
    if args.boundary:
        boundary, err = _read_document(args.boundary, "boundary document")
        if err is not None:
            return err

    # Availability: report each machine whose reachability changes as the run sees it, and
    # remember the ones that went down. A run that loses a machine it needs fails with the
    # scheduler's "no route" message, which does not mention the machine -- so the reason
    # is added to the failure below.
    unreachable: set[str] = set()

    def report_availability(machine: str, reachable: bool) -> None:
        if reachable:
            unreachable.discard(machine)
            print(f"lc run: {machine!r} is reachable again", file=sys.stderr)
        else:
            unreachable.add(machine)
            print(f"lc run: {machine!r} is unreachable (probe)", file=sys.stderr)

    def report_cadence_slip(skipped: int, budget: float, spent: float) -> None:
        # Said once. The run is not wrong -- it skipped the ticks it could not observe and
        # the clock still tells real time -- but the cadence being asked for is not the one
        # being delivered, and only the caller can fix that.
        print(
            f"lc run: a poll cycle took {spent:.3g}s but the poll period is {budget:.3g}s, "
            f"so {skipped} tick(s) went unobserved; the effective period is the cycle's "
            f"cost. Raise --poll-interval or --seconds-per-tick (or probe less often) if "
            f"this repeats.",
            file=sys.stderr,
        )

    def probe_note() -> str:
        return (
            f" (unreachable at the probe: {', '.join(sorted(unreachable))})"
            if unreachable
            else ""
        )

    def timeout_note(kind: str) -> str:
        # The upstream failure says what happened and that nothing cancelled the
        # instrument; what it cannot say is how *this* CLI's user changes the limit.
        if kind != "op_timeout":
            return ""
        return (
            " (raise the limit with --op-timeout SECONDS or the environment root's"
            " x-labcode.op_timeout, or drop it entirely with --no-op-timeout)"
        )

    try:
        # Validation already ran at the front doors above, so run trusting. `run_labcode`
        # owns the labcode Object-identity setup (rewrite the workflow's Object types to
        # declare `_id`, mint the boundary's Object ids, share one IdGenerator with the
        # backend); it runs the rewritten document in memory (no temp file).
        result = run_labcode(
            workflow_doc,
            # The environment document read above, not its path: the dialect front door
            # already parsed it, and the runner takes a document (ofplang-run >= 0.1.13),
            # so the file is read once. The plan's `meta.environment` reads `<in-memory>`
            # as a result, which nothing in a run consumes.
            env_doc,
            boundary,
            # None keeps the runner's default, which is the poll interval: a margin of at
            # least one tick is what stops an overrunning operation's successor from being
            # dispatched onto a resource it still holds.
            running_task_margin=args.margin,
            random_seed=args.seed,
            poll_interval=args.poll_interval,
            # 0 is "no limit" here as it is upstream; the runner spells that None.
            max_ticks=args.max_ticks or None,
            seconds_per_tick=args.seconds_per_tick,
            speed=args.speed,
            observation_out=args.observation_out,
            probe=not args.no_probe,
            on_availability_change=report_availability,
            on_cadence_slip=report_cadence_slip,
            # Three layers, outermost first: this flag, the environment, the default.
            # `FROM_ENVIRONMENT` is "the flag says nothing"; None is "no limit at all".
            op_timeout=(
                None if args.no_op_timeout
                else FROM_ENVIRONMENT if args.op_timeout is None
                else args.op_timeout
            ),
        )
    except (yaml.YAMLError, ContractSyntaxError) as exc:
        print(f"lc run: invalid input: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (SimulatorError, RunnerError) as exc:
        print(f"lc run: execution failed: {exc}{probe_note()}", file=sys.stderr)
        return EXIT_FAILED

    write_err = None
    if args.boundary_out:
        write_err = _write(
            serialize_document(result.result_boundary), args.boundary_out, "result boundary"
        )

    write_err = _emit(result.status, args.output) or write_err
    if result.failed:
        failure = result.failure
        if failure is not None:
            print(
                f"lc run: execution failed: {failure.kind}: {failure.detail}"
                f"{probe_note()}{timeout_note(failure.kind)}",
                file=sys.stderr,
            )
        else:
            print(f"lc run: execution failed: an activity failed{probe_note()}", file=sys.stderr)
        return EXIT_FAILED
    return write_err or EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
