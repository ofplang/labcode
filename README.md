# labcode

[![CI](https://github.com/ofplang/labcode/actions/workflows/ci.yml/badge.svg)](https://github.com/ofplang/labcode/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/labcode.svg)](https://pypi.org/project/labcode/)

The **`lc`** command-line interface for the **labcode** dialect of the
**Object-flow Programming Language**. Installing this one package pulls in the
ofplang toolchain and exposes it under a single command:

```sh
lc validate ...   # check a workflow is well-formed portable v0
lc schedule ...   # compute a schedule for a workflow
lc run ...        # execute a workflow (rolling-horizon runner / simulator)
```

labcode is where a site-specific dialect and a custom runner (real lab hardware)
are developed on top of the ofplang toolchain. At this version `lc` is a thin
dispatcher that forwards each subcommand to the ofplang siblings **unchanged**;
the dialect and custom-runner behavior will land in later versions, replacing
individual subcommands behind the same interface.

## Install

```sh
pip install labcode
```

Requires Python 3.10+. This package is a thin dispatcher; it depends on the
ofplang sibling packages that do the work:

- [`ofplang-validate`](https://github.com/ofplang/validate) — the validator
- [`ofplang-schedule`](https://github.com/ofplang/schedule) — the scheduler
- [`ofplang-run`](https://github.com/ofplang/run) — the runner / simulator

The language is defined in the [ofplang/spec](https://github.com/ofplang/spec)
repository.

## Usage

Each subcommand keeps its own options, exit codes, and `--help`:

```sh
lc --help            # top-level help
lc <command> --help  # command-specific options
lc --version
```

`lc` can also be run as a module: `python -m labcode <command> ...`.

## License

MIT
