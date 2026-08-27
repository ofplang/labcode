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
- **recording a run** — with `--trace`, what the run did is recorded as OpenTelemetry
  traces: one trace per run, a span per operation, and — measured inside the process that
  issued them — a span per SiLA2 connection, per command, and per gRPC call each of those
  made. Off by default; the extra is `pip install labcode[otel]`.

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
    [--ignore-resources] [--trace] [--mission-id ID] [--object-ids seeded|real]
```

- `<workflow>` — the portable v0 workflow: *what* happens.
- `--env` (required) — the labcode environment: the execution environment (spec §5) plus
  the `x-labcode` extension saying *how* each operation is carried out.
- `--boundary DOC` — the whole-workflow I/O as one document: a `boundary:` mapping with a
  `{spot, view}` descriptor per entry input / final output port. `spot` says where a
  boundary Object sits; `view` supplies an input's value. A workflow with Object-bearing
  entry inputs needs one, since each must be placed on a spot and only the operator knows
  where the labware is. Where a device declares a consumable and some mode draws on it,
  an `inventories: {levels: ...}` section says what each stock holds **at the start of
  the run** — the level later on is never stated, it is worked out from that and what the
  run has done since. It is not echoed into `--boundary-out`, because that document is
  written to be fed back and the next run would take this run's opening stock for its
  own.
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
- `--ignore-resources` — switch the consumable model off. The environment's resource
  declarations are still checked for shape but none is applied, so a bench whose devices
  declare stocks nobody is tracking runs without the boundary saying what they held.
- `--trace` — record what the run did (see below). Off by default.
- `--mission-id ID` — the campaign this run belongs to. Recorded with the run and given no
  meaning by labcode: several runs may share one, and nothing here reads it back.
- `--object-ids seeded|real` — how Object `_id`s are minted: `seeded` is reproducible (the
  same workflow yields the same ids every run, which is what keeps the examples' recorded
  output stable), `real` is unique per run. Unset, it follows `--trace`.

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

### Refilling a stock

Where a device declares a consumable and the environment says a replenisher can reach it,
a stock that would run out is **topped up instead of ending the run**. The procedure goes
on the `replenishments[]` route — the pair is what has a procedure, while the machine has
only an address, the same division `transporters` and `transports` have:

```yaml
replenishers:
  - id: dispenser
    x-labcode:
      connection: { kind: sila2, host: 10.0.0.9, port: 50055, insecure: true }

replenishments:
  - replenisher: dispenser
    device: reader
    duration: 4                 # ticks: the scheduler's estimate of the visit
    x-labcode:
      script:
        language: python
        code: |
          import time
          time.sleep(80)        # real seconds: what the visit actually takes
```

The script is handed `replenisher`, `device` and the `amounts` the scheduler derived, and
is expected to put that in. It is **not** handed the duration: a real refill takes as long
as it takes, so a stand-in says so in its own code — which is why the two numbers above
are written separately. Like a transport script it returns nothing; it acts.

A route with no script runs as a timed visit: both machines are held for the declared
duration and nothing is commanded. That is a real thing to write (an operator tops the
stock up while the schedule waits for them) and an easy one to write by accident, so it is
warned about.

`flavor: sila2` is **refused on a refill route** for now: a sila2 script is handed clients,
and which machine's clients a refill should receive — the replenisher's, or both ends' as a
transport may ask for — is not settled. Use `python`.

A refill holds the device it fills *and* the replenisher filling it, so it never overlaps
the work it feeds. It is recorded (`--trace`) as a `replenishment` span naming both
machines.

### Recording a run

`--trace` records what the run did as OpenTelemetry traces. It needs the extra, in the
interpreter that drives the run — which is also the one that runs the scripts, since
labcode launches each with `sys.executable`:

```sh
pip install 'labcode[otel]'
lc run <workflow> --env <env> --trace --mission-id M-2026-001
```

One run is one trace, and the id it can be found by is printed to stderr as the run
starts (`lc run: recording this run as trace …`). What it holds:

```text
run                                  mission.id, and the failure if it stopped on one
├─ process Seal                      which node, process and mode; the plan's interval;
│  │                                 which Objects it handled
│  ├─ sila2.connect                  the address, measured in the process that connected
│  │  └─ /…/SiLAService/GetFeatureDefinition   one per feature, × however many
│  └─ sila2 SealerControl.Seal       the command, from its start to its real completion
│     └─ /…/SealerControl/Seal       the round trip that started it
└─ transport                         the route and the transporter
```

An operation's span is opened when it is dispatched and closed when a poll finds it
finished, so its end is late by up to one poll period; what an instrument actually spent is
in the command spans, which are measured where the commands are issued. `ofp.object.id`
lists the `_id`s an operation handled, including one it created — which is what makes
"everything that happened to this plate" a single query.

The innermost layer is the gRPC calls themselves, each under the connection or the command
that issued it — so what a connection spends is broken down into the feature definitions it
had to fetch, and a command's span separates its round trip from the time the instrument
then took. It needs `grpcio`, which arrives with the `sila2` extra; without it the record is
the same minus that layer. Two things it does not do: an observable command's
execution-info subscription is **not** recorded (it is read on a thread of `sila2`'s own,
where it would land in a trace of its own, and its duration is the command's anyway), and
each recorded call **sends the trace context to the instrument** in its gRPC metadata, which
a SiLA2 server ignores as it does any key that is not SiLA Client Metadata.

Where the record goes is configured by the **standard `OTEL_*` environment variables**
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME`, …), so
labcode adds no settings of its own; `service.name` falls back to `labcode` if nothing sets
it. `LC_TRACE_FILE=path` additionally writes the spans to a file as JSON lines, one file per
process (the name gets the process id), which is how a record can be read without standing
up a collector.

Two things to know before pointing a run at a collector. **`--trace` makes Object `_id`s
real rather than reproducible** unless `--object-ids` says otherwise, since a reproducible id
is the same on every run and would collapse several runs' plates into one. And **a collector
that is configured but does not answer delays each operation**, by as long as
OpenTelemetry's own export timeout allows: labcode sets no timeout of its own, so
`OTEL_EXPORTER_OTLP_TIMEOUT` is the knob, and it belongs to whoever pointed the run there.

## Examples

[`examples/`](examples/README.md) holds three worked runs: `plate_line`, an
Object-bearing line driven entirely by environment scripts and runnable with no
hardware, and `sila2_seal` and `sila2_plate_cycle`, which drive the reference lab's
SiLA2 servers for real. `sila2_seal` also walks through what a run does when a
machine stops answering — before an operation, and in the middle of one.

## License

MIT
