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
— as in a v0 §22 `python_script_processes` script. External `import` is allowed; there is
no sandbox.

**Partial outputs.** Unlike v0 §22.2 (which requires the script to return *every* output
exactly), a labcode process script `return`s only the outputs it **computes** — a subset.
The backend fills the rest:

- an Object output declared in `objects.map` is **carried from its input** (the same
  Object, its view unchanged) — so a pass-through need not restate it;
- any other unset output gets a **typed default** for its type.

The script's returned values override these. Each returned value must conform to its port's
type, and **returning a name that is not a declared output is an error** (this catches a
typo'd output name). A §22.2-strict script — one that returns every output explicitly —
works unchanged.

So for `read` (input `plate`, outputs `plate` via `objects.map` + `od`), all three are
equivalent to returning `{"plate": plate, "od": 0.42}`… except the defaulted forms:

```python
return {"plate": plate, "od": 0.42}   # explicit
return {"od": 0.42}                    # plate carried by objects.map
return {}                              # plate carried; od defaults to 0.0
```

### 1.3 `x-labcode` on a transport route

An environment `transports[]` route may carry an `x-labcode` with a `script`: the Python
that physically carries out that move (e.g. commanding a robot arm). Same shape as §1.1
(`language: python`, string `code`).

```yaml
transports:
  - transporter: arm
    from: reader.stage
    to: sealer.stage
    duration: 3
    x-labcode:
      script:
        language: python
        code: |
          grip = "gentle" if (view or {}).get("fragile") else "firm"
          move_plate(from_spot, to_spot, grip=grip)
```

**Calling convention (transport).** The script runs as a function body with these locals:
`from_spot`, `to_spot`, `transporter` (the physical route), and `view` — the view value of
the moved Object. `view` is **best-effort and MAY be `None`** (the runner resolves it from
the producing arc; when it cannot, it is `None`), so a script that reads it should tolerate
`None`. A transport script is **side-effect only**: its return value is ignored and no
output is verified. Success is "it ran without raising"; an exception is a graceful failure
(the move ends `failed`, no material is moved — the run stops).

A route with no `x-labcode.script` runs as a plain timed move — the runner's material
bookkeeping only, with no device command (a warned no-op for a real move, from != to).

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

- **Device / transporter `x-labcode`** — connection and availability information
  (e.g. SiLA2 address) consolidated on `devices[]` / `transporters[]`, used for a
  connect/command/disconnect wrapper and for `down_devices` availability probing.
