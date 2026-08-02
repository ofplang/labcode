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
- [`sila2_seal.env.yaml`](sila2_seal.env.yaml) drives a plate sealer over SiLA2 and a transport
  arm's `Pick`/`Place`. `cycle_count` is **a real reading**: the script asks the instrument how
  many cycles it has performed, and the number goes up because this run performed one.
- [`sila2_seal.boundary.yaml`](sila2_seal.boundary.yaml) puts the Plate in and takes it out at
  the *same* spot, so the run is a **round trip** and can be repeated without anyone putting
  the world back.

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

> **Only some instruments can be named by a portable workflow yet.** v0 identifiers are
> `[A-Za-z_][A-Za-z0-9_]*`, so a device id cannot contain a hyphen — which rules out the
> reference lab's `seal-remover`, `thermal-cycler` and `trolley-arm`. This example uses
> `plateloc` for that reason. (The arm is fine: labcode names the transporter `arm` and never
> names the arm's own spot.)
