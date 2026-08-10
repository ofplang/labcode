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
  and a transport arm's `Pick`/`Place`, written the **recommended** way — `flavor: sila2`.
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

Spot names still have to be the ones the lab declares — a name it does not know fails the move
at the moment of use. See [`../SPECIFICATIONS.md`](../SPECIFICATIONS.md) §1.4 and §1.6.

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

### Prerequisites

**Verified against [ofplang-sila2-backend](https://github.com/kaizu/sila2-demo) v0.3.0 (commit
`0c3c4c8`)** — a virtual lab of mock SiLA2 instrument servers. That lab is a *reference, not a
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
> rules out the reference lab's `seal-remover`, `thermal-cycler` and `trolley-arm`. This
> example sidesteps it by using `plateloc`; [`sila2_plate_cycle`](#sila2_plate_cycle--the-whole-circuit-four-instruments-and-one-plate)
> below cannot, and translates the names in its transport scripts instead. (The arm is fine
> either way: labcode names the transporter `arm` and never names the arm's own spot.)

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

### Two things this example has to work around

**Hyphens.** The lab's locations are `seal-remover.stage` and `thermal-cycler.block`, which no
v0 identifier can spell. The environment names those devices `seal_remover` and
`thermal_cycler`, and each transport script writes the lab's names out literally rather than
passing `from_spot` / `to_spot` through:

```yaml
code: |
  # labcode's plateloc.stage -> thermal_cycler.block.
  arm = sila2_client.TrolleyArmProvider
  arm.Pick(LocationSpecifier="plateloc.stage")
  arm.Place(LocationSpecifier="thermal-cycler.block")
```

A route is one fixed pair of spots, so there is nothing to compute, and the name the lab
actually receives is the name written in the file. Deriving it instead — `_` → `-` — would
read as a rule, and it is not one: it is a coincidence of this lab's naming, and a real lab may
have a device legitimately called `foo_bar`. The cost is that the route and the strings can
drift apart if one is edited without the other; the lab is what catches that, since a name it
does not know fails the move with `unknown_location`.

Only transport scripts face this, because only they name spots. The lab is *not* renamed to
suit labcode: the dependency runs one way, and the lab's world model is not labcode's to edit.

**Lids and doors.** In the lab's world model a closed lid or door makes that spot inaccessible,
and an item cannot be moved into or out of an inaccessible spot. The natural place to open the
thermal cycler is the transport that delivers the plate to it — but a transport script is given
a client for its **transporter only**, not for the devices at either end of the route, so it
cannot issue `OpenLid` at all. Each process script therefore does close → operate → open,
leaving the instrument as it found it.

That works because a spot's `accessible` defaults to true and the lab's seed closes nothing, so
everything is open at t=0, and because every script here reopens what it closed. Its limit is
worth knowing: **a run that dies between the close and the reopen leaves the spot closed**, and
the next run's transport into that instrument fails with `location_locked`. Reseeding clears
it. This is a workaround for what a transport script is handed, not the intended shape.

### Run it

```sh
python examples/run_sila2_plate_cycle.py
```

Same prerequisites as `sila2_seal` (the lab up, `sila2` importable, the world at t=0), and the
same conventions: exit code 0 means every check passed, and `--artifacts DIR` keeps the run's
status, observation and result boundary.

Verified against both of the reference lab's timing profiles at `--seconds-per-tick 1.0`.
Against `command_durations.realistic.yaml` — the profile the environment's durations are
measured on — the circuit takes a makespan of about 89, and each instrument step lands on its
declared duration; against the default profile (which waits for nothing) every op finishes
early and the run still completes.

Those durations are the **measured operation times, not the instrument's**. An op also costs
labcode a child process and a SiLA2 client (which fetches every Feature definition), and one
poll interval per `settle` that finishes mid-interval — together 2–3 s plus ~1 s per settle.
`thermal_cycle` shows it most: 14 s of instrument time becomes 22, because all six of its
commands are short. Declaring the instrument's time alone would under-run every op, and the
scheduler would keep trying to dispatch a successor onto a device still finishing.

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
(makespan 89); every op runs out-of-process on a wall clock against real servers, so the exact
times vary between runs while the sequence, the identities and the produced values do not.

### Run every SiLA2 example

```sh
python examples/run_all_sila2_examples.py
```

Runs each environment above in turn — both `sila2_seal` environments and `sila2_plate_cycle`
(they are round trips that leave every instrument open, so they follow one another without
intervention) — prints a pass/fail summary, and exits non-zero if any failed. Only the examples
that need the lab are included; `render_plate_line.py` needs nothing but Python.
