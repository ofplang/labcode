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

The extension answers two different questions, in two kinds of place. On a **process
mode**, a **transport route** and a **replenishment route** it says *what to run* (a
`script`, §1.1–§1.4); on a **device**, a **transporter** and a **replenisher** it says
*how to reach the machine* (a `connection`, §1.5), which is what lets a script be the
commands alone (§1.7 for SiLA2, §1.10 for LADS OPC UA). The division is the same each time: the machine has an address,
and the thing it does to something else has a procedure. Nowhere else — see §1.8.

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

- `x-labcode` MUST be a mapping. On a process mode, a transport route or a replenishment
  route its only key is `script`.
- `x-labcode.script`, if present, MUST be a mapping with:
  - `language`: MUST be `python`.
  - `code`: MUST be a string (an implementation-provided Python function body).
  - `flavor` (optional, default `raw`): MUST be `raw`, `sila2` or `lads` — how `code` is
    meant to be run. `sila2` (§1.7) is the **recommended** way to drive a SiLA2 lab, and
    `lads` (§1.10) a LADS OPC UA one: the code is the commands alone and labcode supplies
    the clients. `raw` is the whole function body, written by its author — the general
    escape hatch, and what a script that connects for itself (or speaks some other
    protocol) uses. On a **replenishment route** `sila2` and `lads` are errors in this
    version (§1.4).
  - `endpoints` (**transport routes only**, optional, default `false`): MUST be a boolean —
    whether this move is also given clients for the devices at either **end** of its route,
    not only its `transporter` (§1.7). A process mode may not declare it: a mode's machines
    are the ones it lists. A `sila2` or `lads` script on a route with **no transporter**
    (§1.3) MUST declare it `true`: the ends are then the only machines there are, and a script is never
    given a machine it did not ask for.

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

**A route with no transporter.** An environment may declare a route that needs no
transporter at all, by writing `transporter: null` (ofplang-schedule §4.6 / §5.4) — a
device shifting material between its own spots, a chute. The plan then reports the move
with `transporter: null` too, and this dialect matches it like any other: routes are keyed
by `(transporter, from, to)`, so a null one matches a null one, and the script's
`transporter` local is `None`.

Such a move is performed by the **devices at either end of the route** — for a move within
one device, that device. So a `flavor: sila2` (or `lads`) script on such a route must
declare
**`endpoints: true`** (§1.6), and at least one of those devices must declare a
`connection`; either missing is a front-door error. The `endpoints` request is *required*
rather than inferred: it is the author's statement of which machines the script drives, and
reading it for them would decide that on exactly the routes where it matters most. A `raw`
script, or no script at all, is unaffected — neither is handed clients.

`sila2_client` is then the first machine connected to, which is the **source device**: the
one performing the move, exactly as it is the transporter on a carried route (§1.7).

### 1.4 `x-labcode` on a replenishment route

An environment `replenishments[]` route may carry an `x-labcode` with a `script`: the
Python that physically refills that device from that replenisher (e.g. commanding a
dispenser). Same shape as §1.1 (`language: python`, string `code`).

The procedure lives on the **route**, not on the machine — the same division transports
and transporters have. A dispenser's address is a property of the dispenser (§1.5); how it
fills *this* device is a property of the pair.

```yaml
replenishments:
  - replenisher: dispenser
    device: reader
    duration: 4                    # ticks: the scheduler's estimate of the visit
    x-labcode:
      script:
        language: python
        code: |
          import time
          time.sleep(80)           # real seconds: what the visit actually takes
```

**Calling convention (replenishment).** The script runs as a function body with these
locals: `replenisher`, `device` (the two machines the visit holds) and `amounts` — the
`{resource: amount}` the scheduler derived, which a planned refill fills to the device's
capacity. It is **not** given the duration: a real refill takes as long as it takes, and
the ticks the plan reserved are the scheduler's estimate rather than an instruction, so a
stand-in states its own time (which is why the two numbers above are written separately).

A replenishment script is **side-effect only**, as a transport's is: its return value is
ignored and no output is verified. An exception is a graceful failure — the refill ends
`failed` and the run stops, like any activity failure.

`flavor: sila2` is **an error** on a replenishment route in this version, and so is
`flavor: lads`. Such a script is handed clients (§1.7, §1.10), and which machine's clients a refill should receive — the
replenisher's, or both ends' as a transport route may ask for — is not settled. Refusing
says so; running the script without the clients it asked for would not. Use `raw` (the
default), which may of course connect for itself.

A route with no `x-labcode.script` runs as a plain timed visit: both machines are held for
the declared duration and nothing is commanded. That is a legitimate environment to write
— an operator tops the stock up while the schedule waits for them — and an easy one to
write by accident, so it is **warned** about, as a scriptless real move is.

### 1.5 `x-labcode` on a device, a transporter or a replenisher

An environment `devices[]`, `transporters[]` or `replenishers[]` entry may carry an
`x-labcode` with two keys: `connection` — **where that machine is**, written once per
physical machine rather than repeated in every script that drives it — and `probe` (§1.6)
— whether to check that it still answers. The three kinds are treated alike because they
are alike: each is a machine with an address that a run may find unreachable.

```yaml
devices:
  - id: plateloc
    spots: [stage]
    x-labcode:
      connection: { kind: sila2, host: 127.0.0.1, port: 50053, insecure: true }

  - id: thermal_cycler
    spots: [block]
    x-labcode:
      connection:
        kind: lads
        host: 127.0.0.1
        port: 4844
        insecure: true
        device: ThermalCycler   # optional: which LADS device on that server...
        unit: Cycler            # ...and which of its functional units (§1.10)

transporters:
  - id: arm
    x-labcode:
      connection: { kind: sila2, host: 127.0.0.1, port: 50057, insecure: true }
```

| field | required | default | meaning |
|---|---|---|---|
| `kind` | no | `sila2` | the protocol: `sila2` or `lads` (LADS OPC UA) |
| `host` | **yes** | — | a non-empty string |
| `port` | **yes** | — | an integer in 1..65535 |
| `insecure` | no | `false` | connect without TLS (for `lads`: with OPC UA security mode None) |
| `device` | no | the only one | `lads` only: the browse name of the LADS device on the server |
| `unit` | no | the only one | `lads` only: the browse name of that device's functional unit |

`device` and `unit` are a **LADS** address and nothing else: on a `sila2` connection either
one is an error, and each MUST be a non-empty string. Omitted, they mean *the only one there
is* — a server with one device of one unit, which is the common case, needs neither — and
the omission is resolved when the script connects, not at the front door, since only the
server knows what it holds (§1.10).

**TLS is not supported in this version** — for `lads`, no OPC UA security mode other than
None. There is nowhere in the schema to put the credentials either needs (a root
certificate, a client certificate), so a `connection` whose effective
`insecure` is false is rejected at the front door — including one that simply omits the
key and takes the default — and refused again if a script reaches the connect helper
directly. Every connection must therefore say `insecure: true` today.
The default stays `false` so that supporting TLS later is a pure addition: new fields,
and the error goes away. The check applies to every declared `connection`, whether or not
a script uses it.

**A `sila2` or `lads` script needs somewhere to connect** — a `connection` of its own
`kind` (checked at the front door):

- a mode script with `flavor: sila2` requires **at least one** of that mode's `devices[]`
  to declare a `sila2` `connection`, and one with `flavor: lads` a `lads` one;
- a transport script requires that route's `transporter` to declare one of the script's
  kind — or, on a route with **no transporter** (§1.3), requires `endpoints: true` and at
  least one of the devices at its ends to declare one, those being the machines that
  perform such a move.

A transport that declares `endpoints: true` is also handed the clients of the devices at
either **end** of its route (§1.7), but those are *not* required to declare a `connection`:
a route through a plain holding location is ordinary, and the end without an address is
simply not connected to (a **warning** when *neither* end has one, since then the request
does nothing — an **error** on a route with no transporter, which has nothing else to
drive). An end whose `connection` is of the **other** kind is not connected to either, and
is warned about: a script speaks one protocol (§1.10). The transporter is the one that must be reachable, because it is the machine that
does the moving — and the one `sila2_client` names; where there is none, that is the source
device, for the same reason. Asking a `raw` script for endpoint
clients is an **error**: a raw script is handed no clients at all, so the request cannot be
honoured.

Declaring a `connection` on a device no script connects to is allowed — it is how an
environment is prepared before the scripts that use it are written.

### 1.6 Availability — `probe`

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

### 1.7 Calling convention (`flavor: sila2`)

A `sila2` script is the **commands alone**: labcode opens a client to each of the
operation's machines, runs the code with them in scope, and closes them afterwards. On top
of the input ports of §1.2 (or the transport locals of §1.3), the code sees:

| name | meaning |
|---|---|
| `sila2_clients` | the clients by **machine id**, in the order the operation names its machines: a mode's `devices[]` order, or — for a transport — its `transporter` (absent on a route that has none, §1.3), followed by the devices at either **end of the route** when it declares `endpoints: true`. Named, not held: a mode declaring `device_access: false` (ofplang-schedule §4.4.2) rests on its devices rather than occupying them, and its script is still handed their clients |
| `sila2_client` | the first of them — for a transport its `transporter`, or the **source device** on a route with none (§1.3); the one name a single-machine operation needs |

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

  On a route with **no transporter** (§1.3) a `sila2` script must ask: the ends are the only
  machines there are, so not asking leaves nothing to open, and the front door says so rather
  than letting the move fail when it runs. It is still asked for, not assumed — which
  machines a script drives is the author's to state, and nowhere more so than where the
  machine doing the moving is also the one holding the material.

  ```yaml
  transports:
    - transporter: arm
      from: plateloc.stage
      to: thermal_cycler.block
      duration: 43
      x-labcode:
        script:
          language: python
          flavor: sila2
          endpoints: true        # ...so the lid can be opened before the plate arrives
          code: |
            from labcode.sila2_commands import settle

            cycler = sila2_clients["thermal_cycler"].AutomatedThermalCyclerController
            settle(cycler.OpenLid(), "OpenLid")
            # The transporter is still the first client. Its own names for the places it
            # serves are stations, not `device.spot`, so a route writes them out.
            labware = sila2_client.LabwareService
            settle(labware.Transfer(SourceStation="Base4", DestinationStation="Base6"), "Transfer")
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

#### 1.7.1 `labcode.sila2_commands` — the polling loop, written once

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
  is to turn a hang into a diagnosable failure.
- **It is the inner of two limits.** This one is per command, chosen by the script that
  knows what it is waiting for, and its failure can name the command that hung. The outer
  one (§1.9) is per operation and lab-wide, and catches the hangs no script is watching
  for. The outer default is looser than this one, so where both apply this is what fires.
- **Its timeout is in real seconds**, and is unrelated to the mode's `duration` — which is
  an *estimate*, in environment time, for scheduling. A schedule's estimate is not a
  deadline, and `--seconds-per-tick` does not rescale the timeout.
- **Passing an unobservable command's response is an error** (`sila2_not_observable`): such
  a command has already finished when its call returns, and there is nothing to settle.
- A `sila2` script is only interpreted where the dialect is — in an environment
  `x-labcode`. A workflow's own `script` (v0 §22) has no `flavor`.

### 1.8 Where an `x-labcode` may appear

The positions of §1 are the only ones: the environment **root** (`probe` defaults and
`op_timeout`), `processes.<p>.modes[]`, `transports[]`, `replenishments[]`, `devices[]`,
`transporters[]` and `replenishers[]`. An `x-labcode`
anywhere else in the environment — on a process, beside `time` — is an **error**, as is a
key at a position that does not define it (a `connection` at the root, a `probe` on a mode).
Nothing would read it, and `ofplang-schedule` tolerates an `x-` key at *every* position
without interpreting it, so a misplaced block would otherwise stay silent forever.

This rule covers the environment only. An `x-labcode` in the **workflow** is not reported:
that document is portable v0, read by other implementations, and what extension keys it
carries is not labcode's business.

### 1.9 Operation timeout — `op_timeout`

How long **one operation** may run before labcode stops waiting for it, in **real
seconds**. It lives at the environment root and nowhere else:

```yaml
x-labcode:
  op_timeout: 7200      # a positive number of real seconds (the default)
  # op_timeout: null    # or: wait as long as it takes
```

- `op_timeout` MUST be a positive, finite number, or `null` for **no limit**. `0` is not a
  way to say "no limit" and is an error; a machine may not declare one (a per-machine key
  is an unknown key, §1.1).
- **One value for the whole lab.** The fine-grained waits belong to the scripts, which know
  what they are waiting for (`settle`, §1.7.1); this value only has to clear the longest
  operation the lab legitimately runs. Its default (7200 s) is twice the `settle` default,
  so where both apply the inner one — which can name the command — fires first.
- **The clock is real seconds**, from the moment the operation starts, covering everything
  it does: connecting, every command it issues, and its own waiting. It is unrelated to the
  mode's `duration` (an *estimate*, in environment time, for scheduling) and is not
  rescaled by `--seconds-per-tick`.
- **What happens when it expires**: the operation's child process is stopped and the
  operation **fails** with the reason code `op_timeout`. That is an ordinary graceful
  failure — the run stops, the status document is written, the reason is reported, the exit
  code is 1 — which is the point: without a limit, an instrument that stops answering
  leaves a run polling with *no* status document and no reason at all.
- **A timeout is not a cancel**, exactly as in §1.7.1: nothing here can stop a command the
  instrument has already accepted. It keeps running, and the state that leaves behind —
  including material a transport was part way through moving — is the operator's to
  restore. The run stops there, so labcode's own picture of the lab is not relied on
  afterwards.
- The machine that hung is **not** treated as unavailable: `op_timeout` does not add it to
  the down machines (§1.6), because "not answering" is not "not there", and re-routing work
  onto other machines while this one is still physically running its command would make the
  lab less consistent, not more.
- `lc run` overrides it for one run: `--op-timeout SECONDS`, or `--no-op-timeout` for no
  limit at all. The order is flag, then document, then default.

### 1.10 Calling convention (`flavor: lads`)

A `lads` script is the LADS OPC UA counterpart of a `sila2` one (§1.7), and everything §1.7
says of the flavor holds for it with the names changed: which machines an operation is
handed and in what order, the `endpoints` request on a transport, connections lasting one
operation and closed on any exit, a machine held without a client explaining itself when
indexed (`lads_not_connected`, `lads_endpoints_not_requested`), and the rest of the work
being the script's own. The two names it sees are:

| name | meaning |
|---|---|
| `lads_clients` | a `LadsUnit` for each machine, by **machine id**, in the order of §1.7 |
| `lads_client` | the first of them — for a transport its `transporter`, or the source device on a route with none |

Both are reserved, as `sila2_clients` / `sila2_client` are, and only in a `lads` script.

```yaml
x-labcode:
  script:
    language: python
    flavor: lads
    code: |
      # `lads_client` is the sealer's functional unit, already connected.
      lads_client.write_target("SealingTemperature", 170)
      lads_client.run_program("StartCycle")   # returns once the run has ended
      return {"cycle_count": int(lads_client.read("CycleCount"))}  # a vendor variable
```

- **What a script is handed is one functional unit, not a raw client.** A SiLA2 client is
  typed by its server's features, so a `sila2` script reads well with nothing in between; an
  OPC UA client is not, and a script driving LADS through it directly would be mostly
  namespaces, NodeIds and Variants. A `LadsUnit` is a thin, synchronous view of the unit
  named by the connection's `device` / `unit` (§1.5), built only on what the LADS companion
  specification (OPC 30500, LADS 1.0.0) defines:

  | call | LADS |
  |---|---|
  | `run_program(template_id, **properties)` | `StartProgram`, then wait for the run's result in `ProgramManager/ResultSet`; returns its properties (§1.10.1) |
  | `start_program(template_id, **properties)` | `StartProgram` alone; returns the run id |
  | `results()` | the result nodes in `ProgramManager/ResultSet` |
  | `stop()`, `abort()`, `clear()` | the unit state machine's methods, each waiting until the unit has settled; returns that state |
  | `reset()` | `abort()` then `clear()`, failing unless the unit ends `Stopped` — the counterpart of a SiLA2 `Reset` |
  | `state(owner="FunctionalUnitState")` | a state machine's `CurrentState` (`Stopped`, `Running`, …) |
  | `cover(name, action)` | a cover function's `Open` / `Close`, waiting until it has settled, and failing unless it ends `Opened` / `Closed` |
  | `write_target(function, value)` | a control function's `TargetValue` |
  | `read(path)`, `write(path, value)`, `child(path)` | any node below the unit, by browse names joined with `/`, in whichever namespace they live |
  | `call(owner, method, *variants)` | any LADS method of the object at `owner` |
  | `raw` | the asyncua client itself, for anything none of this covers |

  A method call or write the server refuses fails the operation (`lads_call_failed`). Where
  the server adds a `LastError` variable to the unit — not part of LADS; the reference lab's
  servers do — the failure quotes it, since a StatusCode says *that* the server refused and
  not *why*.
- **Which unit is resolved when connecting.** labcode looks for LADS devices under the
  server's DI `DeviceSet` and for functional units in the chosen device's
  `FunctionalUnitSet`, recognising each by its type (a subtype counts). A `device` or `unit`
  that is named but absent fails the operation (`lads_target_not_found`), as does an omitted
  one where the server has several (`lads_ambiguous_target`, naming them) and a server that
  is not a LADS server at all (`lads_not_a_lads_server`). The front door cannot check any of
  this: it needs the server.
- **One protocol per script.** An operation may hold machines of both kinds — a LADS sealer
  loaded by a SiLA2 arm — but a script is handed clients of its own flavor only. A held
  machine whose `connection` is of the other kind is treated as one held without a client,
  and indexing it says so (`lads_other_protocol`, or `sila2_other_protocol` the other way
  round). A mode none of whose devices is of the script's kind is an error at the front door
  (§1.5); an operation that must command both kinds is written `raw`.
- **The client library is the `lads` extra** (`asyncua`), needed by the interpreter that
  runs the scripts. labcode imports it only when a `lads` script connects, so it runs
  without it otherwise; a `lads` script without it fails with `lads_unavailable`.
- **Probing is unchanged** (§1.6): it opens a TCP connection, whatever the `kind`.
- **A run's record** (`lc run --trace`) holds a `lads` operation's span, but nothing inside
  it yet: the connection and the commands are traced for SiLA2 only (§5).

#### 1.10.1 `labcode.lads_commands` — the waits, written once

A LADS method returns once the server has **begun** carrying it out, and the effect follows:
`StartProgram` returns a run id while the program runs, `Stop` leaves the unit `Stopping`, a
cover passes through `Opening`. So the waits are the script's, as `settle` is in §1.7.1, and
labcode ships them in an ordinary module. A script normally reaches them through its unit
(`lads_client.run_program(...)`, `lads_client.stop()`), which calls them; importing them
reserves nothing, as with `settle`.

- `run_program(unit, template_id, *, timeout=3600.0, poll=1.0, **properties)` starts the
  program and waits for the result whose `DeviceProgramRunId` (or, failing that, browse
  name) is the run id. The **result** is the end of a run, not the unit's state returning to
  `Stopped`: a server records it once the run is over and its effects are visible. The
  properties are passed as LADS' `KeyValueType[]`, stringified.
- **A run that did not complete fails the operation** (`lads_program_failed`). LADS does not
  say how a result reports failure, so labcode reads an `Outcome` property where the server
  records one (anything but `Completed` is a failure) and otherwise the unit's state (a unit
  left `Aborted` failed).
- `wait_for_state(unit, *, owner="FunctionalUnitState", label, timeout=600.0, poll=0.2)`
  waits until a state machine has left every transient state (`Stopping`, `Aborting`,
  `Clearing`, `Starting`, `Opening`, `Closing`) and returns the state it settled in, or fails
  with `lads_transition_timeout`.
- **A timeout is not a cancel**, as in §1.7.1 — though here it could be: LADS has `Abort`.
  A wait that times out (`lads_program_timeout`) still fails only the operation and leaves the
  instrument as it is, so the same mistake leaves the lab in the same state whichever
  protocol the instrument speaks. A script that wants the abort calls `abort()` itself.
- The timeouts are real seconds, the inner limit under `op_timeout` (§1.9), and unrelated to
  a mode's `duration`, exactly as `settle`'s are.

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

**Transport and replenishment routes have no such chain.** There is nothing for them to
fall back to: a workflow describes neither a physical move nor a refill, so the route's
own `x-labcode.script` is the only source. A route without one runs as a plain timed
activity (§1.3, §1.4), warned about for the same reason as above.

## 3. Execution model

Each dispatched operation runs in its own child process (real, wall-clock-paced); the
runner discovers completion by polling, so a multi-minute computation never blocks it.
The advisory `duration` is the scheduler's estimate; the real duration is the script's.
A script error (an exception, a wrong/ missing output name, a non-conformant value) is a
graceful runtime failure (§22.2): the operation ends `failed` and the run stops. An
operation that never finishes at all ends the same way once it passes `op_timeout` (§1.9) —
polling for completion is not the same as waiting forever for it.

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
(§1.6) can add seconds — so the margin wants to be generous rather than exact. A run whose
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

- **`flavor: sila2` or `lads` on a replenishment route** — refused today (§1.4). What has
  to be settled first is which machine's clients a refill script receives: the
  replenisher's alone, or both ends' as a transport route may ask for with `endpoints`.
  Until then a refill that must speak SiLA2 or LADS uses a `raw` script and connects for
  itself.
- **A deeper probe** — asking a machine something (a SiLA2 property read) rather than only
  opening a connection to it, so "answering" can be checked and not just "listening"
  (§1.6). It would be an opt-in depth, since it costs a real exchange per check.
- **Probing in parallel** — checking machines concurrently, so a lab with many unreachable
  machines does not pay for them one timeout at a time (§1.6).
- **TLS** — the fields a secure connection needs, lifting the restriction in §1.5; for
  LADS, an OPC UA security policy and the certificates it takes.
- **Tracing a `lads` operation** — spans for the connection and each method call inside an
  operation's span, as a `sila2` one has (§1.10).
- **Aborting on a timeout, as an opt-in** — LADS can cancel a run where SiLA2 cannot, so a
  `lads` wait could abort what it gave up on. It is off (§1.10.1) until it can be asked for
  per call, since the state an abort leaves is not always better than the one a run leaves.
