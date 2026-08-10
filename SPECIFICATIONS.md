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
§1.4), which is what lets a script be the commands alone (§1.6). Nowhere else — see §1.7.

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
    be run (§1.6). `sila2` is the **recommended** way to drive a SiLA2 lab: the code is the
    commands alone and labcode supplies the clients. `raw` is the whole function body,
    written by its author — the general escape hatch, and what a script that connects for
    itself (or speaks something other than SiLA2) uses.
  - `endpoints` (**transport routes only**, optional, default `false`): MUST be a boolean —
    whether this move is also given clients for the devices at either **end** of its route,
    not only its `transporter` (§1.6). A process mode may not declare it: a mode's machines
    are the ones it lists.

**Unknown keys are an error** — in `x-labcode` at every position, and in the mappings it
holds. A key this version does not know is either a typo or a feature it does not have;
either way, ignoring it would mean a document that says one thing and a run that does
another (a misspelled `flavour:` running unwrapped, a `probe:` on a mode monitoring
nothing). This applies only inside `x-labcode` — the workflow's own `script` (v0 §22)
belongs to `ofplang-validate`.

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

An environment `devices[]` or `transporters[]` entry may carry an `x-labcode` with two
keys: `connection` — **where that machine is**, written once per physical machine rather
than repeated in every script that drives it — and `probe` (§1.5) — whether to check that
it still answers.

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
key and takes the default — and refused again if a script reaches the connect helper
directly. Every connection must therefore say `insecure: true` today.
The default stays `false` so that supporting TLS later is a pure addition: new fields,
and the error goes away. The check applies to every declared `connection`, whether or not
a script uses it.

**A `sila2` script needs somewhere to connect** (checked at the front door):

- a mode script with `flavor: sila2` requires **at least one** of that mode's `devices[]`
  to declare a `connection`;
- a transport script with `flavor: sila2` requires that route's `transporter` to declare
  one.

A transport that declares `endpoints: true` is also handed the clients of the devices at
either **end** of its route (§1.6), but those are *not* required to declare a `connection`:
a route through a plain holding location is ordinary, and the end without an address is
simply not connected to (a **warning** when *neither* end has one, since then the request
does nothing). The transporter is the one that must be reachable, because it is the machine
that does the moving — and the one `sila2_client` names. Asking a `raw` script for endpoint
clients is an **error**: a raw script is handed no clients at all, so the request cannot be
honoured.

Declaring a `connection` on a device no script connects to is allowed — it is how an
environment is prepared before the scripts that use it are written.

### 1.5 Availability — `probe`

A machine that stops answering should not keep receiving work. A `probe` policy asks labcode
to check the machines it knows how to reach, and to tell the scheduler about the ones it
cannot: their process modes, and the transports they carry or touch, are dropped from the
environment the scheduler sees, so the run **routes around them**.

`probe` may be written on a device or a transporter, and at the **environment root** as a
document-wide default. The root's fields sit under a machine's own, **field by field**, so
a machine can change one thing without restating the rest.

| field | default | meaning |
|---|---|---|
| `enabled` | `false` | whether this machine is probed at all |
| `timeout` | `5` | how long one check may take, in **real seconds** |
| `interval` | `once` | `once` (check at the start of the run and keep that answer), a number of **real seconds** to re-check on, or `0` to re-check on every replan |

`timeout` and `interval` are real seconds — probing is work done against the real world, so
it has nothing to do with the environment's time unit or the run's wall-clock pacing.

**Writing a policy does not enable it.** `enabled` defaults to false wherever it is not
said, so adding an `interval` to an environment cannot start probing something that was not
being probed before; an environment with no `probe` at all behaves exactly as it did before
this version. A policy that nothing enables is a **warning** — it does nothing, which is
unlikely to be what its author meant.

**A probed machine needs an address.** A machine whose effective policy is enabled must
declare a `connection` — otherwise there is nothing to probe, and that is an error. This
matters when enabling probing document-wide, because the root reaches *every* machine: a
plain holding device with no connection then has to be excluded on purpose.

```yaml
# Per machine: enable only the ones with an address (nothing to write for a holding device)
devices:
  - id: plateloc
    spots: [stage]
    x-labcode:
      connection: { kind: sila2, host: 127.0.0.1, port: 50053, insecure: true }
      probe: { enabled: true, interval: 60 }
  - { id: station, spots: [slot1] }

# Document-wide: enable once, and exclude what cannot be reached
x-labcode:
  probe: { enabled: true, interval: 60 }
devices:
  - id: plateloc
    spots: [stage]
    x-labcode:
      connection: { kind: sila2, host: 127.0.0.1, port: 50053, insecure: true }
  - id: station
    spots: [slot1]
    x-labcode:
      probe: { enabled: false }
```

**What a probe is.** Opening a TCP connection to the declared address, and nothing more. It
needs no client library, and it establishes **reachability, not readiness**: a machine whose
port is open but whose software is wedged reads as up here, and that case surfaces where it
belongs — as the operation that tried to command it failing.

**What it does to a run.**

- Only **new scheduling** is affected. An operation already running on a machine that has
  just gone down is not touched.
- **If there is no other way, the run fails.** A workflow that needs a machine nothing can
  replace stops with the scheduler's "no route" error rather than dispatching onto it. That
  error names an arc or a mode, not a machine, so `lc run` appends the machines the probe
  found unreachable — otherwise the answer to "why is there no route" is not in the message.
- **Recovery is automatic** — for a policy that re-checks. With `once` (the default) the
  first answer stands for the whole run; with an `interval`, a machine that comes back
  returns to the plan.
- **A check costs run-loop time**, and that cost is subject to §3.1 below: probing happens
  in the process driving the run, one machine at a time, on the replan that asks for it.
  This is the loop's most expensive optional step, so it is the likeliest thing to make a
  cycle outgrow its poll period — which is why `interval: 0` is a setting for a diagnosis
  rather than for operating a lab.
- **What costs is the machine that is *not* answering.** A reachable machine answers in well
  under a millisecond on a local network. An unreachable one is only cheap when something
  actively refuses the connection; a machine that was switched off, that left the network, or
  that sits behind a host holding the port open while nothing serves it takes up to its
  `timeout` to read as down. The cost of a round therefore follows the machines that are
  down, not the ones that are up — so the round to size the poll period against (§3.1) is
  the one in which the most of them are.
- `lc run --no-probe` ignores the policies and treats every machine as reachable (the
  document is still validated, so an environment that is wrong about probing stays wrong).
  Each machine whose reachability changes is reported on stderr.

### 1.6 Calling convention (`flavor: sila2`)

A `sila2` script is the **commands alone**: labcode opens a client to each of the
operation's machines, runs the code with them in scope, and closes them afterwards. On top
of the input ports of §1.2 (or the transport locals of §1.3), the code sees:

| name | meaning |
|---|---|
| `sila2_clients` | the clients by **machine id**, in the order the operation holds its machines: a mode's `devices[]` order, or — for a transport — its `transporter`, followed by the devices at either **end of the route** when it declares `endpoints: true` |
| `sila2_client` | the first of them — for a transport always its `transporter`; the one name a single-machine operation needs |

```yaml
x-labcode:
  script:
    language: python
    flavor: sila2
    code: |
      # `sila2_client` is already connected to this mode's device.
      return {"od": sila2_client.OpticalDensityProvider.MeasureOD().OD}
```

- **These two names are reserved.** A script's inputs are bound as its function's
  parameters, so an input port of the same name would be silently overwritten by a client;
  a process that declares one is rejected at the front door (as a `_id` view field is,
  §4.1).
- **A transport may be handed all three of the machines it holds** — `endpoints: true`. A
  transport activity occupies the source device, the destination device *and* the transporter
  for its whole body (`ofplang-schedule` SPECIFICATIONS §4.5), so all three are its to
  command: that is what lets the move that needs a lid open be the move that opens it, and
  nothing else can be using either instrument meanwhile, because the scheduler has given them
  both to this move.

  It is **off unless asked for**, per route. A move that drives nothing but its transporter
  should pay for one connection rather than three, and should not begin to fail because an
  instrument it merely hands a plate to is switched off — while needing to open a lid is a
  property of the move, not of the lab. A route that does not ask still *holds* both ends, so
  reaching for one is answered with what to add rather than with silence.

  ```yaml
  transports:
    - transporter: arm
      from: plateloc.stage
      to: thermal_cycler.block
      duration: 8
      x-labcode:
        script:
          language: python
          flavor: sila2
          endpoints: true        # ...so the lid can be opened before the plate arrives
          code: |
            from labcode.sila2_commands import settle

            cycler = sila2_clients["thermal_cycler"].AutomatedThermalCyclerController
            settle(cycler.OpenLid(), "OpenLid")
            arm = sila2_client.TrolleyArmProvider   # the transporter: still the first client
            arm.Pick(LocationSpecifier="plateloc.stage")
            arm.Place(LocationSpecifier="thermal-cycler.block")
  ```
- **Connections last one operation**, opened before the code runs and closed after it — on
  any exit, including a `return` or an exception, and including a *later* connection
  failing after an earlier one opened. There is no pooling and no reconnection: reaching an
  instrument is assumed, and failing to is an ordinary operation failure naming the machine.
  A machine that declares a `connection` is connected to whether or not the script uses it,
  so the cost of an operation follows the machines it **holds**, not the ones it commands.
- **A machine held without a client explains itself.** An operation may hold a machine it
  cannot reach — a plain holding device declares no `connection` — and that machine is
  absent from `sila2_clients` (`in`, `.get()` and iteration all say so). *Indexing* it is
  different: it yields a stand-in that is **falsy**, so `if sila2_clients[id]:` reads as "is
  there a client for it", and that fails the operation with **why** there is none
  (`sila2_not_connected`, or `sila2_endpoints_not_requested` for an end of a route that did
  not ask for it) if the script commands it anyway. Indexing an id the operation does not
  hold at all raises instead, naming what it does hold: that is a typo, and a falsy stand-in
  would let it survive until something stranger happened later.
- **Everything else is still the script's own.** The flavor supplies connections, nothing
  more: waiting for an observable command to finish (the standard `sila2` polling pattern)
  belongs in the code, as it does in a `raw` script.

#### 1.6.1 `labcode.sila2_commands` — the polling loop, written once

Waiting for an observable command is the same loop in every script that issues one, so
labcode ships it. It is an **ordinary module**, reached by an ordinary import — nothing is
injected, and a script that does not import it does not have it:

```yaml
code: |
  from labcode.sila2_commands import settle

  feature = sila2_client.PlateLocController
  settle(feature.StartCycle(), "StartCycle")
  return {"cycle_count": int(feature.CycleCount.get())}
```

`settle(instance, label, *, timeout=3600.0, poll=1.0)` polls `instance` until it reports
`done` and returns its `get_responses()`.

This is deliberately *not* part of the calling convention above. A name that appears out of
nowhere is worth spending only on what a script cannot obtain for itself — a live connection
is that, an import is not — and keeping it an import means the helper reserves no name, is
equally available to a `raw` script, and stays visible in the code that depends on it.

- **A timeout is not a cancel.** SiLA2 offers no way to stop a command already issued, so a
  `settle` that times out fails the *operation* while the instrument carries on. Whatever
  state that leaves the lab in is the operator's to restore, as for any operation that
  failed part way. The default timeout is therefore generous rather than tight: its purpose
  is to turn a hang into a diagnosable failure, since nothing else in the stack bounds an
  operation's running time.
- **Its timeout is in real seconds**, and is unrelated to the mode's `duration` — which is
  an *estimate*, in environment time, for scheduling. A schedule's estimate is not a
  deadline, and `--seconds-per-tick` does not rescale the timeout.
- **Passing an unobservable command's response is an error** (`sila2_not_observable`): such
  a command has already finished when its call returns, and there is nothing to settle.
- A `sila2` script is only interpreted where the dialect is — in an environment
  `x-labcode`. A workflow's own `script` (v0 §22) has no `flavor`.

### 1.7 Where an `x-labcode` may appear

The positions of §1 are the only ones: the environment **root** (`probe` defaults only),
`processes.<p>.modes[]`, `transports[]`, `devices[]` and `transporters[]`. An `x-labcode`
anywhere else in the environment — on a process, beside `time` — is an **error**, as is a
key at a position that does not define it (a `connection` at the root, a `probe` on a mode).
Nothing would read it, and `ofplang-schedule` tolerates an `x-` key at *every* position
without interpreting it, so a misplaced block would otherwise stay silent forever.

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

Cadence: the nominal poll period is `poll_interval × seconds_per_tick`. labcode defaults
`seconds_per_tick` to ~20 s (so a real op is polled at an observable cadence, not
sub-second, which would flood the replan loop); `lc run --seconds-per-tick/--speed/
--poll-interval/--margin` override it.

**The running-task margin defaults to the poll interval.** When the scheduler replans, a
still-running operation is pinned to end at `max(reported end, now + margin)` — the margin
is how far ahead of *now* an operation that has not finished is assumed to run for. A
positive margin is therefore what keeps that operation's successor from being planned at
`now` and dispatched onto a resource it has not released; and a real operation overruns its
estimate as a matter of course, so labcode defaults the margin to one poll interval rather
than to 0. Note that this does not depend on the cadence holding (§3.1): the pin moves with
`now`, so skipped ticks cannot erode the protection.

### 3.1 A poll cycle has to fit its poll period

One turn of the loop costs the driving process real time: replanning, dispatching, and
whatever else the dialect does before it waits again. Call that the **cycle cost** and the
nominal poll period the **budget**. The relation between them decides how the run behaves,
and there is no third case:

- **cost < budget** — the loop waits out the difference, so a turn takes exactly the budget
  and the clock lands on the next tick. The cost is *absorbed*: the run keeps the cadence it
  was asked for and each operation's recorded duration reflects the lab. This is the case
  the defaults are chosen for, and the case a lab should run in.
- **cost > budget** — there is nothing left to wait for. The loop stops waiting, the ticks
  it could not observe are **skipped**, and the clock jumps to the tick real time has
  reached. Nothing is falsified by that: the clock still tells real time, and the lab really
  did keep running while the loop was busy. But the *effective* period becomes the cycle
  cost, so `poll_interval` and `seconds_per_tick` no longer set the cadence, and every
  operation's recorded duration is rounded up to that coarser grid — a fast operation can be
  recorded as having taken a whole cycle. **`lc run` reports the first slip** (how long the
  cycle took, what the period was, how many ticks went unobserved), because the fix is a
  setting only the caller can change.

The report comes from the wait, so a cycle that never reaches it says nothing. When the
replan at the top of a cycle fails outright — there is no route, because the work needs a
machine that is gone — the run ends there, and no slip is reported however long that cycle
took. The absence of the message means the loop never got as far as waiting; it is not a
statement that the cycle was cheap.

So: keep the budget comfortably larger than the cycle cost. What the cycle costs is not
fixed — replanning grows with the workflow, and a dialect step such as availability probing
(§1.5) can add seconds — so the margin wants to be generous rather than exact. A run whose
recorded times matter (a checked-in example, a comparison against the plan's estimates)
needs this to hold; a run that only has to *complete* does not.

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

- **A deeper probe** — asking a machine something (a SiLA2 property read) rather than only
  opening a connection to it, so "answering" can be checked and not just "listening"
  (§1.5). It would be an opt-in depth, since it costs a real exchange per check.
- **Probing in parallel** — checking machines concurrently, so a lab with many unreachable
  machines does not pay for them one timeout at a time (§1.5).
- **TLS** — the fields a secure connection needs, lifting the restriction in §1.4.
