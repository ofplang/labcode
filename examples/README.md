# labcode examples

## `plate_line` — an Object-bearing line, driven by environment scripts

A `Plate` flows down a four-station line, a `Tube` is dispensed into it, and a
measurement is read off it:

```
         (in) tube ──────────────┐
                                  ▼
load ──[move]──> dispense ──[move]──> read ──[move]──> store
                     │                   │
                (out) tube           (out) od
```

- [`plate_line.workflow.yaml`](plate_line.workflow.yaml) is **portable ofplang v0** — it
  says only *what* happens. The whole workflow takes one `Tube` in and passes the *same*
  Tube back out, alongside the Pure Data measurement `od`. `dispense` and `read` are
  **Object-bearing**: their Objects go in and the *same* Objects come out (`objects.map` —
  identity preserved). The Plate is created inside the workflow by `load`; the Tube enters
  at the run boundary.
- [`plate_line.boundary.yaml`](plate_line.boundary.yaml) is the **run boundary** (dev-notes
  D28). It supplies the `Tube` input — an Object, so it names a `spot`: a standalone tube
  `rack` slot bound to no process, where the Tube starts and returns (`Tube` has no view
  fields, so no `view`). The arm carries it to the dispenser and back. It also names the
  outputs `od` and `tube`, whose produced values are echoed in the result boundary.
- [`plate_line.env.yaml`](plate_line.env.yaml) is the **labcode environment** — it says
  *how* each step is carried out, as an `x-labcode.script` (see [`../SPECIFICATIONS.md`](../SPECIFICATIONS.md)).
  **Every process mode and every transport route carries a script**: `load` makes the
  Plate, `dispense` dispenses the Tube into the Plate (both Objects are carried through by
  `objects.map`, so its script returns nothing), `read` measures `od` (the Plate is
  carried, so the script returns only what it computes — labcode's *partial outputs*, see
  the spec), `store` takes it; the transport scripts perform the move and may read the
  moved Plate's `view` (its barcode).

labcode runs each script **out-of-process** on a wall clock, discovering completion by
polling — so a real, slow device operation never blocks the runner.

### Run it

```sh
lc run examples/plate_line.workflow.yaml --env examples/plate_line.env.yaml \
  --boundary examples/plate_line.boundary.yaml --seconds-per-tick 0.2
```

`--seconds-per-tick` sets the real seconds per environment time tick; it defaults to a
coarse value suited to real hardware, so a small value here keeps the demo quick. The run
completes with every activity `completed`; the produced measurement (`od = 0.42`) can be
written out with `--boundary-out result.yaml`.

### Produce the outputs

[`render_plate_line.py`](render_plate_line.py) drives the same run on the labcode backend
and writes its artifacts under [`outputs/`](outputs/):

```sh
python examples/render_plate_line.py
```

- [`outputs/plate_line.plan.yaml`](outputs/plate_line.plan.yaml) — the **final execution
  schedule** (the §6/§7 status document: every activity, `completed`).
- [`outputs/plate_line.observation.yaml`](outputs/plate_line.observation.yaml) — the
  **observation document** (D38): each completed activity's I/O views, as `lc run
  --observation-out` would stream it.
- [`outputs/plate_line.svg`](outputs/plate_line.svg) — a **Gantt chart** of that schedule
  (device view), drawn by the scheduler's visualizer. (The ofplang toolchain renders
  SVG/HTML; open it in a browser, or convert to PNG with any SVG rasterizer.)
- [`outputs/plate_line.boundary.yaml`](outputs/plate_line.boundary.yaml) — the **result
  boundary**, echoing the produced `od` and the returned `tube` (as `--boundary-out`
  writes it).

Because the labcode backend runs each op out-of-process on a wall clock, the exact times
(and makespan) may vary slightly between runs; the sequence and produced values do not.

Every Object's view carries a reserved **`_id`** — labcode's implicit, value-layer Object
identity (see [`../SPECIFICATIONS.md`](../SPECIFICATIONS.md) §4). In the observation you can
follow the *same* Plate (one `_id`) from `load` through `dispense`/`read` to `store`, and
the Tube's `_id` round-trips from the input boundary to the output. The ids are
reproducible (a seeded, provenance-keyed generator), so these outputs are stable to
re-generate.

> The scripts here are mocks (they just return values / reference their locals). Replace a
> script's body with real device calls — e.g. `robot.move(from_spot, to_spot)` in a
> transport, or an instrument read in `read` — to drive real hardware; the code may
> `import` anything the host Python can.

## `sila2_seal` — the same idea, driven by real SiLA2 servers

Where `plate_line`'s scripts are mocks that only return values, this example's scripts open
SiLA2 connections and issue real commands. It is the integration check for labcode's SiLA2
story: labcode schedules, dispatches out-of-process, a script talks SiLA2, an instrument acts,
and the produced value comes back through labcode's partial outputs.

```
   (in) plate ──[move]──▶ seal ──[move]──▶ (out) plate
                            │
                    (out) cycle_count
```

- [`sila2_seal.workflow.yaml`](sila2_seal.workflow.yaml) is portable ofplang v0: one Plate in,
  the *same* Plate out (`objects.map`), plus the Pure Data reading `cycle_count`.
- [`sila2_seal.wrapped.env.yaml`](sila2_seal.wrapped.env.yaml) drives a plate sealer over SiLA2
  and a transport arm's `Transfer`, written the **recommended** way — `flavor: sila2`.
  `cycle_count` is **a real reading**: the script asks the instrument how many cycles it has
  performed, and the number goes up because this run performed one.
- [`sila2_seal.boundary.yaml`](sila2_seal.boundary.yaml) puts the Plate in and takes it out at
  the *same* spot, so the run is a **round trip** and can be repeated without anyone putting
  the world back.

Each machine's address is declared **once**, on the device (or the transporter) that has it:

```yaml
devices:
  - id: plateloc
    spots: [stage]
    x-labcode:
      connection: { kind: sila2, host: 127.0.0.1, port: 50053, insecure: true }
```

…and each script is the **commands alone**. labcode opens `sila2_client` before the code runs
and closes it afterwards — on a `return`, on an exception, and on a later connection failing
after an earlier one opened:

```yaml
x-labcode:
  script:
    flavor: sila2
    language: python
    code: |
      from labcode.sila2_commands import settle

      feature = sila2_client.PlateLocController
      settle(feature.StartCycle(), "StartCycle")   # the wait is still the script's own
      return {"cycle_count": int(feature.CycleCount.get())}
```

What the flavor supplies is connections, nothing more. Waiting for an *observable* command to
finish stays in the script — but the loop itself does not have to be rewritten each time:
labcode ships `settle` in **`labcode.sila2_commands`** (§1.6.1), reached by the ordinary import
above. Nothing is injected, so a script that does not import it does not have it, and a `raw`
script has exactly the same access as this one. A live connection is worth a name that appears
out of nowhere; an import is not.

Note what `settle` is *not*. Its timeout is in real seconds, unrelated to the mode's `duration`
(an estimate, for scheduling) and unscaled by `--seconds-per-tick`; and it cancels nothing — a
SiLA2 command cannot be stopped from here, so a timeout fails the operation while the
instrument carries on, leaving the lab for the operator to restore.

The names a script hands a machine still have to be the ones that machine knows — for the arm
those are its **station** names (`Base1`, `Base4`, …), not labcode's `device.spot`, and one it
does not know fails with `InvalidStation` at the moment of use. See
[`../SPECIFICATIONS.md`](../SPECIFICATIONS.md) §1.4 and §1.6, and the plate-cycle section below
for why the two vocabularies do not meet anywhere but in a transport script.

### If a machine stops answering

Neither environment here asks for it, but labcode can **check that a machine is reachable**
and schedule around the ones that are not — see
[`../SPECIFICATIONS.md`](../SPECIFICATIONS.md) §1.5. Adding this to a device (or a
transporter) that declares a `connection`:

```yaml
    x-labcode:
      connection: { kind: sila2, host: 127.0.0.1, port: 50053, insecure: true }
      probe: { enabled: true, interval: 60 }
```

…makes `lc run` report the machine when its reachability changes and plan without it while
it is down. These two examples leave it out on purpose: each has exactly one sealer and one
arm, so there is nothing to route around, and what a stopped lab should produce here is the
instrument command failing where it was issued. `lc run --no-probe` turns probing off for a
run without editing the environment.

### If a machine stops answering *mid-operation*

Probing catches a machine that is not there; it does not catch one that accepted a command
and never came back. That is what the **operation timeout** is for
([`../SPECIFICATIONS.md`](../SPECIFICATIONS.md) §1.8): every operation has a real-seconds
deadline (7200 s by default, declared lab-wide at the environment root as
`x-labcode.op_timeout`), and one that passes it is stopped and failed with the reason
`op_timeout` — a run that ends with a status document and a reason instead of one that
polls forever. A script that knows its own commands should still bound them itself with
`settle(..., timeout=...)`: it fires first and can say *which command* hung. `lc run
--op-timeout SECONDS` / `--no-op-timeout` change the outer limit for a single run.

### Prerequisites

**Verified against [ofplang-sila2-backend](https://github.com/kaizu/sila2-demo) branch `ardea`
(commit `de3c4fd`)** — a virtual lab of mock SiLA2 instrument servers. That branch is where its
transporter became a mock of **Ardea**, a machine that exists, serving the real one's nine
Feature definitions unchanged; it is not merged, so a checkout of `main` will not run these
examples. That lab is a *reference, not a
requirement*: the scripts speak plain SiLA2, so pointing them at real instruments is a matter
of changing the host and port in the environment. The version is recorded so a run without
hardware has something known to reproduce against; it is deliberately not asserted on, since
checking a server's name would be the one thing that stopped this working against hardware.

```sh
# in the reference lab
docker compose up -d

# here: the client library has to be importable by the interpreter that runs the scripts
uv sync --extra sila2
```

The world must be at t=0 — one plate on `station.slot1`. The round trip puts it back, so
repeated runs need no intervention; a run that failed part way may not have, and restoring the
world is the **operator's** job, not the workflow's:

```sh
curl -X POST http://localhost:8001/reseed
```

### Run it

```sh
python examples/run_sila2_seal.py
```

Exit code 0 means every check passed, so this is a check rather than a demo. It asserts only on
what a real client can see — the schedule, the produced boundary, the observation — and **never
contacts the lab's world-state service**: that service stands in for the physical world, which
exposes no such interface, so a client that read it could not be pointed at hardware.

Pass `--artifacts DIR` to keep the run's status, observation and result boundary (they are
otherwise written to a temporary directory, since their timings vary between runs).

### The same run, with each script connecting for itself

[`sila2_seal.env.yaml`](sila2_seal.env.yaml) drives the *same* workflow through the same
motions with `raw` scripts: no `connection` on the devices, each script opening its own
`SilaClient` (so the address appears in every script that talks to a machine, and closing it is
the author's `with`), and its `settle` written out by hand rather than imported — not because a
`raw` script may not import it, but because this file is the low-level reference and the loop
is worth being able to read. It is what to write when the connection is not a plain SiLA2 one,
or when the script needs to do something the flavor does not cover:

```sh
python examples/run_sila2_seal.py --env examples/sila2_seal.env.yaml
```

The two environments are interchangeable today but are **not promised to stay equivalent**; the
`flavor: sila2` one is the example that follows the dialect.

> **A device id cannot contain a hyphen.** v0 identifiers are `[A-Za-z_][A-Za-z0-9_]*`, which
> rules out the reference lab's `seal-remover` and `thermal-cycler`. This example sidesteps it by
> using `plateloc`; [`sila2_plate_cycle`](#sila2_plate_cycle--the-whole-circuit-four-instruments-and-one-plate)
> below renames them to `seal_remover` and `thermal_cycler`. It costs nothing either way now,
> because no script spells a lab location: the arm is told a **station** name, and labcode's
> device ids never reach it.

## `sila2_plate_cycle` — the whole circuit: four instruments and one plate

Where `sila2_seal` drives one instrument, this drives a line. One `Plate` is unsealed,
resealed, thermal-cycled and spun down, carried between the four instruments by the arm, and
returned to the spot it started from:

```
   (in) plate ──▶ peel ──▶ seal ──▶ thermal_cycle ──▶ rotate ──▶ (out) plate
                    │        │            │
           (out) tape_left   │     (out) elapsed_time
                     (out) cycle_count
```

- [`sila2_plate_cycle.workflow.yaml`](sila2_plate_cycle.workflow.yaml) is portable ofplang v0:
  one Plate in, the *same* Plate out, carried through all four steps by `objects.map`. Three
  steps also report a reading; `rotate` reports nothing, because the centrifuge exposes no
  counter — it is the example of a step that only carries its Object.
- [`sila2_plate_cycle.wrapped.env.yaml`](sila2_plate_cycle.wrapped.env.yaml) drives the four
  instruments and the arm with `flavor: sila2`. The command arguments are taken from the
  delivery scripts in `ScriptsForIntegrationTest` (`snip_xpeel.py`, `snip_plateloc.py`,
  `snip_atc.py`, `snip_microplate_centrifuge.py`), so what each instrument is asked to do here
  is what those ask of the real hardware.
- [`sila2_plate_cycle.boundary.yaml`](sila2_plate_cycle.boundary.yaml) puts the Plate in and
  takes it out at `station.slot1`, so the circuit is a **round trip** like `sila2_seal`'s.

Every reading is a real one: the seal remover's remaining supply-spool tape, the sealer's cycle
count, and the thermal cycler's `hh:mm:ss` elapsed time — read after the run, so it is non-zero
exactly because a run happened. And because the Plate's `_id` survives four handovers, the
check can assert something a single-step run cannot: that all four instruments handled the
*same* plate.

This is the labcode counterpart of the reference lab's own `samples/run_roundabout.py`, with
one difference that is the point of it: there, a script drives the circuit directly; here,
labcode schedules it and dispatches each step, and each script only says what one step does.

### Two things worth reading the environment for

**Two vocabularies for the same places.** labcode addresses a place as `device.spot` —
`plateloc.stage` — because that is what a workflow reasons about: which machine holds the
Object. The arm addresses the same places as **stations**: `Base1`, `Base2`, … are the
machine's own names for the positions it can serve, and they are what `Transfer` takes. On the
real Ardea those names index its motion configuration, which records where each station sits on
the rail and which robot tasks reach it — so they are the only thing the arm can be told.

A transport script is where the two meet, and it writes its station names out literally.
Nothing derives one from the other, because no rule does:

```yaml
code: |
  # labcode's plateloc.stage -> thermal_cycler.block = the arm's Base4 -> Base6.
  from labcode.sila2_commands import settle

  labware = sila2_client.LabwareService
  settle(labware.Transfer(SourceStation="Base4", DestinationStation="Base6"), "Transfer")
```

A route is one fixed pair of places, so there is nothing to compute either. The lab's own server
holds the same map (in `ARDEA_STATIONS`) and is what turns a station name into a place. The cost
is that the route and the names can drift apart if one is edited without the other; the lab is
what catches that, since a station name it does not know fails with `InvalidStation`.

Note also that **a transfer is one command but an observable one**, so `settle` is not optional
in a transport script either: without it the script reports success while the plate is still in
the air. That is new — the arm this replaced moved a plate with two *unobservable* commands, so
its transports had nothing to wait for.

A smaller mismatch sits underneath: the device ids are not the lab's device names either, since
v0 identifiers cannot contain a hyphen and the lab's devices are `seal-remover` and
`thermal-cycler`. Hence `seal_remover` and `thermal_cycler`. That one now costs nothing, because
no script spells a lab location any more. The lab is *not* renamed to suit labcode: the
dependency runs one way, and the lab's world model is not labcode's to edit.

**Lids and doors.** In the lab's world model a closed lid or door makes that spot inaccessible,
and an item cannot be moved into or out of an inaccessible spot. So the transport that delivers
the plate is what opens the instrument: three of the five routes declare `endpoints: true` and
are handed clients for the devices at either end as well as for the arm (§1.6). That is sound
because the scheduler has already given the move both instruments for its whole duration —
nothing else can be using them meanwhile.

The convention the environment follows is that **an instrument is closed at rest**: a transport
opens what it must to pick and to place and closes the source it emptied, and a process closes
the instrument to work and leaves it closed. Delivering the plate to the thermal cycler
therefore reads as: the transport opens the lid and places the plate; `thermal_cycle` closes it,
runs, and leaves it closed; the next transport opens it, takes the plate out, and closes it
again.

```yaml
x-labcode:
  script:
    flavor: sila2
    endpoints: true
    code: |
      from labcode.sila2_commands import settle

      cycler = sila2_clients["thermal_cycler"].AutomatedThermalCyclerController
      settle(cycler.OpenLid(), "OpenLid")
      labware = sila2_client.LabwareService   # the transporter: still the first client
      settle(labware.Transfer(SourceStation="Base4", DestinationStation="Base6"), "Transfer")
```

Closed-at-rest is what a real instrument does, and it is what makes this example *check*
something rather than merely work. The lab starts with everything open, so the first run's
`OpenLid` is a no-op — but every run after it starts with the cycler and the centrifuge closed
and gets nowhere unless the transports really can open them. (`OpenLid` / `CloseLid` set the
world's accessibility outright rather than toggling it, so calling one that is already true
costs nothing.) The thing to know is that the plate is inside a closed instrument for part of
the circuit, so a run that dies there leaves it there — an operator's job to retrieve, or a
reseed, as for any run that fails half way.

### Run it

```sh
python examples/run_sila2_plate_cycle.py
```

Same prerequisites as `sila2_seal` (the lab up, `sila2` importable, one plate on
`station.slot1`), and the same conventions: exit code 0 means every check passed, and
`--artifacts DIR` keeps the run's status, observation and result boundary. It needs no
particular lid or door state to start from: the transports open what they need.

Verified against both of the reference lab's timing profiles at `--seconds-per-tick 1.0`.
Against `command_durations.realistic.yaml` — the profile the environment's durations are
measured on — the circuit takes a makespan of about 260–290 and each step lands within its
declared duration; against the default profile (which waits for nothing) every op finishes
early and the run still completes. The realistic run takes about five minutes of wall clock,
most of it the arm: a `Transfer` is 30 s in that profile, and there are five of them.

Those durations are the **measured operation times, not the instrument's**. An op also costs
labcode a child process (~2 s), a SiLA2 client per machine it connects to, and one poll interval
per `settle` that finishes mid-interval. All three show: `thermal_cycle` turns 10 s of
instrument time into 17 because all five of its commands are short; the cycler → centrifuge move
costs 51 for 41 s of instrument time, opening two instruments and closing one across three
clients; and **the arm's client is the expensive one** — ~1 s buys a single-feature instrument,
but Ardea serves nine Feature definitions and a client fetches all of them, measured 3–7 s.
Every route pays that, which is why a plain move is 40 and not 33. Declaring the instrument's
time alone would under-run every op, and the scheduler would keep trying to dispatch a successor
onto a device still finishing.

### Produce the outputs

[`render_sila2_plate_cycle.py`](render_sila2_plate_cycle.py) drives the same circuit and keeps
its documents under [`outputs/`](outputs/), so the schedule can be read — and its Gantt chart
looked at — without a lab to hand:

```sh
python examples/render_sila2_plate_cycle.py
```

- [`outputs/sila2_plate_cycle.plan.yaml`](outputs/sila2_plate_cycle.plan.yaml) — the **final
  execution schedule** (the §6/§7 status document): nine activities, five transports and four
  instrument steps, every one `completed`.
- [`outputs/sila2_plate_cycle.observation.yaml`](outputs/sila2_plate_cycle.observation.yaml) —
  the **observation document** (D38). Follow one `_id` through `peel` → `seal` →
  `thermal_cycle` → `rotate` and back to the output boundary: that is the plate surviving four
  handovers.
- [`outputs/sila2_plate_cycle.svg`](outputs/sila2_plate_cycle.svg) — a **Gantt chart** of that
  schedule (device view), drawn by the scheduler's visualizer. Six rows — the four instruments,
  the station and the arm — and the makespan marker.
- [`outputs/sila2_plate_cycle.boundary.yaml`](outputs/sila2_plate_cycle.boundary.yaml) — the
  **result boundary**: the three readings and the returned plate.

It is a producer, not a check: it asserts nothing, and `run_sila2_plate_cycle.py` is what says
whether the example still works. Unlike `render_plate_line.py` it needs the lab, since its
scripts issue real commands. The committed copies were produced on the realistic profile
(makespan 261); every op runs out-of-process on a wall clock against real servers, so the exact
times vary between runs — by tens of seconds, now that five 30 s transfers dominate — while the
sequence, the identities and the produced values do not.

## `sila2_plate_cycle_no_atc` — the same circuit, on a bench with no thermal cycler

The full circuit above needs an automated thermal cycler. When there is not one to use, the
workflow that goes to the bench is this one — `sila2_plate_cycle` with that step removed and the
rest in the order a bench with an **unsealed** plate can run them:

```
   (in) plate ──▶ seal ──▶ rotate ──▶ peel ──▶ (out) plate
                    │                    │
          (out) cycle_count        (out) tape_left
```

Seal, spin, peel — so the plate ends the circuit in the state it began it, and the run repeats
without anyone reconditioning a plate in between. That is the same property the round trip has
for the plate's *location*, applied to its condition; peeling first would need a sealed plate to
start from and hand back a sealed one.

- [`sila2_plate_cycle_no_atc.workflow.yaml`](sila2_plate_cycle_no_atc.workflow.yaml) is the
  circuit minus one node, with the remaining three in the new order: the plate is sealed, spun
  down and unsealed, and the *same* plate comes back to `station.slot1`.
- [`sila2_plate_cycle_no_atc.wrapped.env.yaml`](sila2_plate_cycle_no_atc.wrapped.env.yaml)
  drops the `thermal_cycler` device, its routes and the `thermal_cycle` mode, and joins four
  different pairs of places: to the sealer, to the centrifuge, to the peeler, home. Only the
  centrifuge has a door, so only the two routes at its ends open anything. Every instrument
  script and every duration is the full circuit's, unchanged.
- [`sila2_plate_cycle_no_atc.boundary.yaml`](sila2_plate_cycle_no_atc.boundary.yaml) is the
  full circuit's boundary without `elapsed_time` — that reading was the cycler's.

Nothing connects to the cycler, so the run does not care whether that server is up: the check
asserts it was never scheduled, which is what makes a pass here evidence for the bench it was
written for rather than for the full lab.

```sh
python examples/run_sila2_plate_cycle_no_atc.py
```

Same prerequisites and conventions as `sila2_plate_cycle` (the lab up, `sila2` importable, one
plate on `station.slot1`, exit code 0 means every check passed, `--artifacts DIR` keeps the
documents). It checks the same things minus the cycler's reading: every activity completed,
four transports and three instrument steps, both readings real, the plate home at
`station.slot1`, and all three instruments handling the plate with the same `_id`.

Verified against the reference lab on both timing profiles at `--seconds-per-tick 1.0`. On
`command_durations.realistic.yaml` the circuit is a makespan of about 190–220 (two ops fewer
than the full one's 260–290); on the default profile every op finishes early and the run still
completes. Running it twice in a row is the more interesting pass: the second run starts with
the centrifuge closed by the first, so it only gets anywhere if the transport really can open
the door.

`--trace` records the run (`labcode.record`) and prints its trace id, so the SiLA2 commands, the
client builds and the poll waits *inside* each operation are separable — which is what to look
at when a duration turns out to be wrong. The exporter is configured by OpenTelemetry's own
environment variables:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
  python examples/run_sila2_plate_cycle_no_atc.py --trace --artifacts run1
```

One thing to get right first: **point the exporter at `127.0.0.1`, not `localhost`.** Where the
collector binds IPv4 only, `localhost` can resolve to `::1` first, and the exporter's retry
backoff then stalls whichever operation was exporting. Measured on the reference lab at
`--seconds-per-tick 1.0`: makespan 216 untraced, **219** traced via `127.0.0.1` (so recording
itself costs next to nothing), **279** traced via `localhost` — one 39 s move became 95 s, and
the run reported a 54 s poll cycle. Both traced runs recorded the same 114 spans, so the trace
was not the poorer for it; only the schedule was.

Even so, take a duration from an *untraced* run and use the trace to explain it. Recording puts
an exporter in every operation's child process, and an operation's measured time is exactly what
these numbers are for.

### Run every SiLA2 example

```sh
python examples/run_all_sila2_examples.py
```

Runs each environment above in turn — both `sila2_seal` environments, `sila2_plate_cycle` and
`sila2_plate_cycle_no_atc` (they are round trips that put the plate back where it started, and
each opens whatever it needs open, so they follow one another without intervention) — prints a
pass/fail summary, and exits non-zero if any failed. Only the examples
that need the lab are included; `render_plate_line.py` needs nothing but Python.

## Taking one of these to a bench

The examples above point at the reference lab because it is what CI and a laptop can run. An
environment that speaks plain SiLA2 goes to real instruments by **changing hosts and ports and
nothing else** — so a bench environment is best written as a copy of the mock one with the
addresses replaced, and nothing else touched. Then `diff` says what is bench-specific.

`*.remote.env.yaml` is **git-ignored**: this repository is public, and a bench's host/port
inventory is not something to publish by accident. Keep the copy local, or commit it
deliberately.

### Check the bench before commanding anything

[`preflight_sila2_env.py`](preflight_sila2_env.py) takes any labcode environment and checks it
against the machines it names, **issuing no SiLA2 command**:

```sh
python examples/preflight_sila2_env.py --env examples/<your>.remote.env.yaml
```

Everything it does is a read — building labcode's own client (which fetches every Feature
definition, and is where TLS, a wrong port and a half-open server show up), the `SiLAService`
properties every server serves, and the transporter's non-observable
`CarriageService.StationNames`. Nothing moves, nothing opens, nothing starts. It reports

- each machine's identity and the Features it serves;
- whether every Feature the environment's **scripts** name is actually served — a script
  reaching for a Feature the server does not implement otherwise fails mid-run, with a plate
  somewhere;
- whether every station name the transport scripts use is one the transporter **knows** — an
  unknown one fails with `InvalidStation`.

What it cannot check is the one thing the hardware will not catch either: whether a station
name the machine *does* know is the place the workflow means. `Base4` that is the sealer on one
bench and the cycler on another does not fail — it puts a plate somewhere else. That mapping
has to be confirmed against the bench, and it is the thing to confirm before a first run.
