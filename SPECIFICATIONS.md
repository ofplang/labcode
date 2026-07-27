# labcode dialect specification

labcode is a dialect of the Object-Flow Programming Language (ofplang). A labcode
workflow **is** a portable v0 ofplang workflow; the dialect lives entirely in the
**execution environment** (§5), as an `x-labcode` extension that says *how* each device
operation is physically carried out. `lc run` drives the workflow on the labcode backend
(`ofplang.run.SubprocessBackend`), sourcing each operation's script from `x-labcode` and
running it out-of-process on a wall clock.

This document is the reference for the `x-labcode` extension; `labcode.dialect` is its
conformance validator, run at the `lc run` front door.

## 1. `x-labcode` on a process mode (P5)

An environment process mode (§5) may carry an `x-labcode` mapping. In this version it
holds a single key, `script`: the Python that carries out that `(process, mode)`.

```yaml
processes:
  measure_od:
    modes:
      - id: v0
        devices: [reader]
        duration: 45          # the scheduler's estimate; real time is the script's own
        x-labcode:
          script:
            language: python  # the only supported language
            code: |
              return {"od": read_plate(plate)}
```

`x-labcode` is tolerated (and ignored) by `ofplang-schedule` (>= 0.1.2): the environment
still validates and schedules as plain v0. Only labcode interprets it.

### 1.1 Shape

- `x-labcode` MUST be a mapping.
- `x-labcode.script`, if present, MUST be a mapping with:
  - `language`: MUST be `python`.
  - `code`: MUST be a string (an implementation-provided Python function body).

### 1.2 Calling convention (process)

The script runs as the body of a function whose parameters are the operation's **input
port names**, each bound to that port's view value (Pure Data or an Object's view record)
— identical to a v0 §22 `python_script_processes` script. It MUST `return` a mapping of
the declared **output port** names to their view values, each conforming to its declared
type (verified exactly as §22.2). External `import` is allowed; there is no sandbox.

## 2. Code source resolution and exclusivity

For a dispatched `(process, mode)`, labcode resolves the code to run in this order:

1. the mode's `x-labcode.script.code` (2 — the labcode device script), else
2. the workflow process's own `script.code` (1 — a v0 §22 script process), else
3. none — the operation runs as a **typed-default no-op** (its outputs are typed
   defaults; a device not yet scripted).

**Exclusivity (error).** A process MUST NOT carry both a workflow `script` (1) and an env
`x-labcode.script` (2) on any of its modes; that is ambiguous and is rejected.

**Typed-default reachability (warning).** A process with neither (1) nor (2) on any mode
will run as a typed-default no-op. This is allowed — convenient while mocking a device —
but `lc run` warns about it, so an unimplemented device is not silently a no-op.

## 3. Execution model

Each dispatched operation runs in its own child process (real, wall-clock-paced); the
runner discovers completion by polling, so a multi-minute computation never blocks it.
The advisory `duration` is the scheduler's estimate; the real duration is the script's.
A script error (an exception, a wrong/ missing output name, a non-conformant value) is a
graceful runtime failure (§22.2): the operation ends `failed` and the run stops.

Cadence: the effective poll period is `poll_interval × seconds_per_tick`. labcode defaults
`seconds_per_tick` to ~20 s (so a real op is polled at an observable cadence, not
sub-second, which would flood the replan loop); `lc run --seconds-per-tick/--speed/
--poll-interval/--margin` override it.

## 4. Not yet in this version (roadmap)

- **Transport scripts** — an `x-labcode.script` on an environment `transports[]` route,
  run when material is moved. The upstream seam is ready: `Backend.dispatch_transport`
  carries the moved Object's `view` (ofplang-run >= 0.1.5), best-effort and possibly
  `None`; a transport script's locals will be `from_spot`, `to_spot`, `transporter`,
  `view`, with no return value (side-effect only). labcode does not run them yet.
- **Device / transporter `x-labcode`** — connection and availability information
  (e.g. SiLA2 address) consolidated on `devices[]` / `transporters[]`, used for a
  connect/command/disconnect wrapper and for `down_devices` availability probing.
