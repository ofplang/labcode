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
