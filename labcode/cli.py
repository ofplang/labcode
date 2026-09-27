"""labcode ``lc`` command-line interface.

``lc`` is the entry point for the labcode dialect of the Object-flow Programming
Language. It is a thin dispatcher over the ofplang toolchain: each subcommand is
forwarded to a sibling package's own CLI, in-process::

    lc validate ...  ->  ofplang.validate.cli.main
    lc schedule ...  ->  ofplang.schedule.cli.main
    lc run ...       ->  labcode.run_cli.main       (labcode dialect backend)
    lc export ...    ->  ofplang.export.cli.main    (optional: labcode[export])

``validate``, ``schedule`` and ``export`` forward to the ofplang siblings unchanged;
``run`` is the labcode dialect's own entry (`labcode.run_cli`): it drives the workflow
on the labcode backend (env ``x-labcode`` device scripts, run out-of-process on a wall
clock) and adds the dialect front door. The seam -- routing each subcommand
independently -- is where further dialect behavior will diverge. Each subcommand keeps
its own options, exit codes, and ``--help``; ``lc`` adds no behavior of its own
beyond routing plus a top-level ``--help``/``--version``.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

# Subcommand -> dotted path of the sibling CLI module exposing ``main(argv)``.
# Kept as strings so each sibling is imported lazily, only when its subcommand is
# invoked: ``lc --help`` need not import a scheduler's ortools/numpy stack, and a
# subcommand fails with a clear message if its package is somehow missing. This
# dict is also the divergence seam: a later labcode version replaces an entry
# (e.g. ``run``) with its own module to add dialect / custom-runner behavior.
_SUBCOMMANDS: dict[str, str] = {
    "validate": "ofplang.validate.cli",
    "schedule": "ofplang.schedule.cli",
    "run": "labcode.run_cli",
    # Writes documents out for people to read -- the single-file viewer. It reads
    # only what the ofplang specifications define, so it forwards unchanged, like
    # validate and schedule; it is an extra because nothing else needs it.
    "export": "ofplang.export.cli",
}

# Subcommands whose package is an optional extra rather than a dependency, and
# the install that brings it in -- named when the package is missing, as the
# other extras (sila2, lads, otel) are.
_EXTRAS: dict[str, str] = {
    "export": "pip install 'labcode[export]'",
}

_USAGE = """\
usage: lc <command> [options]

The labcode CLI: a dialect wrapper over the Object-flow Programming Language
toolchain. Subcommands forward to the ofplang toolchain.

commands:
  validate    check a workflow document is well-formed portable v0
  schedule    compute a schedule for a workflow
  run         execute a workflow on the labcode backend (env x-labcode scripts)
  export      write documents out for people to read (a single-file viewer)

Run `lc <command> --help` for command-specific options.

options:
  -h, --help     show this help and exit
  -V, --version  show version and exit
"""


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("labcode")
    except PackageNotFoundError:  # editable/source tree without installed metadata
        return "0+unknown"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        sys.stderr.write(_USAGE)
        return 2

    head = args[0]
    if head in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    if head in ("-V", "--version"):
        sys.stdout.write(f"lc {_version()}\n")
        return 0
    if head.startswith("-"):
        sys.stderr.write(f"lc: unrecognized option {head!r}\n\n")
        sys.stderr.write(_USAGE)
        return 2

    module_path = _SUBCOMMANDS.get(head)
    if module_path is None:
        sys.stderr.write(f"lc: unknown command {head!r}\n\n")
        sys.stderr.write(_USAGE)
        return 2

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        sys.stderr.write(
            f"lc: the '{head}' command requires a package that is not installed "
            f"({exc}).\n"
        )
        if head in _EXTRAS:
            sys.stderr.write(f"lc: it is optional; install it with: {_EXTRAS[head]}\n")
        return 2

    exit_code: int = module.main(args[1:])
    return exit_code
