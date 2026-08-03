# labcode dialect specification

labcode is a dialect of the Object-Flow Programming Language (ofplang). A labcode
workflow **is** a portable v0 ofplang workflow; the dialect lives entirely in the
**execution environment** (§5), as an `x-labcode` extension that says *how* each device
operation is physically carried out. `lc run` drives the workflow on the labcode backend
(`ofplang.run.SubprocessBackend`), sourcing each operation's script from `x-labcode` and
running it out-of-process on a wall clock.

This document is the reference for the `x-labcode` extension; `labcode.dialect` is its
conformance validator, run at the `lc run` front door.

## 1. `x-labcode` in an environment (P5)

The extension appears in four places, each answering a different question: on a **process
mode** and on a **transport route** it says *what to run* (a `script`, §1.1–§1.3); on a
**device** and on a **transporter** it says *how to reach the machine* (a `connection`,
§1.4). Nowhere else — see §1.5.

An environment process mode (§5) may carry an `x-labcode` mapping holding a `script`: the
Python that carries out that `(process, mode)`.

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

- `x-labcode` MUST be a mapping. On a process mode or a transport route its only key is
  `script`.
- `x-labcode.script`, if present, MUST be a mapping with:
  - `language`: MUST be `python`.
  - `code`: MUST be a string (an implementation-provided Python function body).
  - `flavor` (optional, default `raw`): MUST be `raw` or `sila2` — how `code` is meant to
    be run. `raw` is the whole function body, written by its author (a script that speaks
    SiLA2 by connecting for itself is `raw`). `sila2` is the command body alone, with the
    client(s) named by the devices' `connection` (§1.4) supplied around it.
    **This version does not interpret `sila2`**: the schema accepts it so an environment
    can be written against it, but the code still runs as written, and `lc run` warns to
    that effect.

**Unknown keys are an error**, both in `x-labcode` and in `script`. A key this version
does not know is either a typo or a feature it does not have; either way, ignoring it
would mean a document that says one thing and a run that does another (a misspelled
`flavour:` running unwrapped, a `probe:` monitoring nothing). This applies only inside
`x-labcode` — the workflow's own `script` (v0 §22) belongs to `ofplang-validate`.

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

### 1.4 `x-labcode` on a device or a transporter

An environment `devices[]` or `transporters[]` entry may carry an `x-labcode` whose only
key is `connection`: **where that machine is**, written once per physical machine rather
than repeated in every script that drives it.

```yaml
devices:
  - id: plateloc
    spots: [stage]
    x-labcode:
      connection: { kind: sila2, host: 127.0.0.1, port: 50053, insecure: true }

transporters:
  - id: arm
    x-labcode:
      connection: { kind: sila2, host: 127.0.0.1, port: 50057, insecure: true }
```

| field | required | default | meaning |
|---|---|---|---|
| `kind` | no | `sila2` | the protocol; `sila2` is the only value this version defines |
| `host` | **yes** | — | a non-empty string |
| `port` | **yes** | — | an integer in 1..65535 |
| `insecure` | no | `false` | connect without TLS |

**TLS is not supported in this version.** There is nowhere in the schema to put the
credentials it needs (a root certificate, at least), so a `connection` whose effective
`insecure` is false is rejected at the front door — including one that simply omits the
key and takes the default. Every connection must therefore say `insecure: true` today.
The default stays `false` so that supporting TLS later is a pure addition: new fields,
and the error goes away. The check applies to every declared `connection`, whether or not
a script uses it.

**A `sila2` script needs somewhere to connect** (checked at the front door, though not yet
acted on):

- a mode script with `flavor: sila2` requires **at least one** of that mode's `devices[]`
  to declare a `connection`;
- a transport script with `flavor: sila2` requires that route's `transporter` to declare
  one.

Declaring a `connection` on a device no script connects to is allowed — it is how an
environment is prepared before the scripts that use it are written.

### 1.5 Where an `x-labcode` may appear

The four positions of §1 are the only ones: `processes.<p>.modes[]`, `transports[]`,
`devices[]` and `transporters[]`. An `x-labcode` anywhere else in the environment — at the
document root, on a process, beside `time` — is an **error**. Nothing would read it, and
`ofplang-schedule` tolerates an `x-` key at *every* position without interpreting it, so a
misplaced block would otherwise stay silent forever.

This rule covers the environment only. An `x-labcode` in the **workflow** is not reported:
that document is portable v0, read by other implementations, and what extension keys it
carries is not labcode's business.

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

## 4. Object identity — the reserved `_id` view key

labcode gives every Object a stable, value-layer identity so it can be traced across
steps and in the observation document. The identity lives in the Object's **view** under
the reserved key **`_id`** (a `String`). This is a dialect feature layered on portable
v0: the workflow the user writes carries no `_id`; `lc run` injects and mints it.

### 4.1 Type rewrite

Before running, `lc run` rewrites the workflow in memory: it adds `_id: { type: String }`
to the `view` of **every `domain: object` type** (creating `view` if the type had none).
`_id` is an ordinary legal v0 view field (a leading-underscore identifier, not reserved
in core, and a primitive `String`), so the rewritten document validates and schedules
unchanged, and the runner's closed-shape view conformance treats `_id` as a normal
declared field. labcode runs this rewritten document directly (no temp file:
`ofplang.run.run_workflow` accepts an in-memory document).

**Reserved (error).** A user type that itself declares a `_id` view field is rejected at
the dialect front door — labcode owns `_id`, and silently clobbering the field would be
worse than a clear error.

### 4.2 Where an id comes from

An Object's `_id` is set at its two points of origin, then **carried** everywhere else —
`objects.map` and transport copy the whole view, so `_id` propagates for free:

- **`objects.create`** — a newly created Object's `_id` is minted when the operation
  produces it (in the backend's output fill). A device script need not know about `_id`:
  it returns only what it computes, and the fill supplies `_id` (like any other unset
  output, §1.2).
- **run boundary** — a whole-workflow Object *input* enters at the boundary; `lc run`
  mints its `_id` (filling any other declared view field with a typed default so the
  seeded value conforms), **unless the boundary already carries one** — so a result
  boundary fed back in round-trips its ids.
- **`objects.map`** — a mapped Object output carries its input's `_id` unchanged
  (identity preserved), even if a §22.2-strict script returned the port explicitly.

### 4.3 Reproducibility

Ids come from a swappable generator (`labcode.idgen.IdGenerator`). The default
(`SeededUuid4Generator`) mints **reproducible** uuid4-shaped ids from a seed and a
*provenance key* — the node instance + output port for a create, the port name for a
boundary input — **not** draw order. So the same workflow yields the same ids on every
run, and the wall-clock backend's jittering completion order cannot change them (which is
what keeps checked-in example observations stable). A real run wanting globally-unique
ids per physical Object swaps in `RealUuid4Generator` (via
`labcode_backend_factory(id_generator=...)`).

> The provenance key is the runner's node-instance identity + port. Today each create
> node runs once, so node-path + port is unique; when dynamic control flow (e.g.
> `do_while`) is added, that node-instance identity must include the iteration index so
> ids stay unique and reproducible.

## 5. Not yet in this version (roadmap)

- **Running a `sila2` script as one** — wrapping the code of a `flavor: sila2` script
  (§1.1) in a connect/disconnect prologue built from the devices' `connection` (§1.4), so
  the script is the command body alone and the client(s) are supplied around it. The
  schema is in place; the execution is not.
- **Availability probing** — a `probe` block (enable / timeout / interval) on a device,
  with a document-wide default at the environment root, driving `down_devices()` so the
  scheduler routes around a machine that cannot be reached. Writing `probe` today is an
  error rather than something ignored (§1.1).
- **TLS** — the fields a secure connection needs, lifting the restriction in §1.4.
