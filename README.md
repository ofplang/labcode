# labcode

[![CI](https://github.com/ofplang/labcode/actions/workflows/ci.yml/badge.svg)](https://github.com/ofplang/labcode/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/labcode.svg)](https://pypi.org/project/labcode/)

The **`lc`** command-line interface for the **labcode** dialect of the
**Object-flow Programming Language**. Installing this one package pulls in the
ofplang toolchain and exposes it under a single command:

```sh
lc validate ...   # check a workflow is well-formed portable v0
lc schedule ...   # compute a schedule for a workflow
lc run ...        # execute a workflow on the labcode backend
```

labcode is where a site-specific dialect and a custom runner (real lab hardware)
are developed on top of the ofplang toolchain. `lc validate` and `lc schedule`
forward to the ofplang siblings unchanged; **`lc run` is the labcode dialect's own
runner**: it drives the workflow on the labcode backend, running each device
operation's script — supplied in the environment as an `x-labcode.script` extension
on a process mode — out-of-process on a wall clock, so a long-running real operation
never blocks the replan loop. See [`SPECIFICATIONS.md`](SPECIFICATIONS.md) for the
`x-labcode` extension.

```yaml
# in the execution environment: how a (process, mode) is carried out
processes:
  measure_od:
    modes:
      - id: v0
        duration: 45
        x-labcode:
          script:
            language: python
            code: |
              return {"od": read_plate(plate)}
```

## Install

```sh
pip install labcode
```

Requires Python 3.10+. `lc validate` and `lc schedule` are dispatched to the ofplang
sibling packages unchanged; `lc run` is this package's own runner, built on them:

- [`ofplang-validate`](https://github.com/ofplang/validate) — the validator
- [`ofplang-schedule`](https://github.com/ofplang/schedule) — the scheduler
- [`ofplang-run`](https://github.com/ofplang/run) — the runner / simulator

The language is defined in the [ofplang/spec](https://github.com/ofplang/spec)
repository, and what labcode adds to it in [`SPECIFICATIONS.md`](SPECIFICATIONS.md).

What `lc run` brings of its own, beyond dispatching:

- **the labcode backend** — each device operation's `x-labcode.script` runs
  out-of-process on a wall clock (§1.2, §1.3), so a real operation that takes minutes
  does not block the replan loop, and an operation that never returns is stopped by
  **`op_timeout`** (§1.8; `--op-timeout` / `--no-op-timeout`).
- **the dialect front door** — the environment's `x-labcode` extension is validated
  before anything runs, on top of the portable-v0 check `lc validate` performs (§1, §2).
- **availability probing** — each machine is checked as often as its `probe` policy says,
  and one that cannot be reached is taken out of the environment the scheduler plans
  against, so the run routes around it (§1.5; `--no-probe`).
- **object identity** — the reserved `_id` view key is declared on Object types and minted
  per object, so a physical thing can be followed through a run (§4).
- **`flavor: sila2`** — a script that speaks SiLA2 gets its clients opened around it
  (§1.6). The client library itself is the `sila2` extra: `pip install labcode[sila2]`,
  installed into whichever interpreter runs the scripts.

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
