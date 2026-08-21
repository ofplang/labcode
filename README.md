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

`lc validate` and `lc schedule` are the ofplang siblings' own CLIs unchanged, so their
options are documented in those repositories. `lc run` is this package's own, and is
described below.

### `lc run`

```sh
lc run <workflow> --env <env>
    [--boundary DOC] [-o OUT] [--boundary-out FILE] [--observation-out FILE]
    [--seconds-per-tick S] [--op-timeout S | --no-op-timeout] [--no-probe]
```

- `<workflow>` — the portable v0 workflow: *what* happens.
- `--env` (required) — the labcode environment: the execution environment (spec §5) plus
  the `x-labcode` extension saying *how* each operation is carried out.
- `--boundary DOC` — the whole-workflow I/O as one document: a `boundary:` mapping with a
  `{spot, view}` descriptor per entry input / final output port. `spot` says where a
  boundary Object sits; `view` supplies an input's value. A workflow with Object-bearing
  entry inputs needs one, since each must be placed on a spot and only the operator knows
  where the labware is.
- `-o OUT` — write the final execution status (spec §6/§7) here; the default is stdout.
- `--boundary-out FILE` — write the result boundary: the same schema as `--boundary`, with
  each produced output's `view` filled in, including the `_id` its Object was minted with
  (§4) — which is how one checks that the plate that came back is the plate that went in.
- `--observation-out FILE` — stream the observation document: each *completed* activity's
  concrete input / output view values, appended as it finishes. What the instruments
  reported, as against the status document's timings.
- `--seconds-per-tick S` — real seconds per environment time tick (default 20). Durations
  in the environment are counted in ticks, and this is what maps them onto the wall clock.
  The default is deliberately coarse, so that a real operation's dispatch → running →
  completed reads as discrete, observable steps; a demo against a fast mock wants a small
  value.
- `--op-timeout S` / `--no-op-timeout` — how long one operation may run before it is
  stopped and failed (§1.8). The default is the environment root's `x-labcode.op_timeout`,
  else 7200 real seconds. The two forms exclude each other.
- `--no-probe` — ignore the environment's `x-labcode.probe` policies and treat every
  machine as reachable (§1.5). The documents are still validated.

Exit codes: `0` the run completed, `1` it failed (an activity failed, a contract was
violated, an operation timed out, or a replan became infeasible), `2` a usage or input
error — including a workflow or an `x-labcode` extension the front doors reject.

A complete invocation, against this repository's `examples/` (`--seconds-per-tick` small
because that example's scripts return instantly):

```sh
lc run examples/plate_line.workflow.yaml --env examples/plate_line.env.yaml \
  --boundary examples/plate_line.boundary.yaml --seconds-per-tick 0.2
```

The remaining options tune the replan loop rather than describe the run —
`--poll-interval`, `--margin`, `--seed`, `--speed`, `--max-ticks`, `--no-validate` — and
are covered by `lc run --help`.

## License

MIT
