# labcode examples

## `plate_line` — an Object-bearing line, driven by environment scripts

A `Plate` flows down a three-station line and a measurement is read off it:

```
load ──[move]──> read ──[move]──> store
```

- [`plate_line.workflow.yaml`](plate_line.workflow.yaml) is **portable ofplang v0** — it
  says only *what* happens. `read` is **Object-bearing**: the Plate goes in and the *same*
  Plate comes out (`objects.map` — identity preserved), together with a Pure Data
  measurement `od`. The Plate is created inside the workflow, so no run boundary is needed.
- [`plate_line.boundary.yaml`](plate_line.boundary.yaml) is the **run boundary** (dev-notes
  D28). Because the Plate is created internally, there are no boundary *inputs* to place;
  it only names the whole-workflow output `od`, so the produced value is echoed back
  explicitly in the result boundary. It is deliberately minimal (an empty `boundary: {}`
  would run the same) — a starting point to grow.
- [`plate_line.env.yaml`](plate_line.env.yaml) is the **labcode environment** — it says
  *how* each step is carried out, as an `x-labcode.script` (see [`../SPECIFICATIONS.md`](../SPECIFICATIONS.md)).
  **Every process mode and every transport route carries a script**: `load` makes the
  Plate, `read` measures `od` (the Plate is carried through automatically by `objects.map`,
  so the script returns only what it computes — labcode's *partial outputs*, see the spec),
  `store` takes it; the transport scripts perform the move and may read the moved Plate's
  `view` (its barcode).

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
  boundary**, echoing the produced `od` (as `--boundary-out` writes it).

Because the labcode backend runs each op out-of-process on a wall clock, the exact times
(and makespan) may vary slightly between runs; the sequence and produced values do not.

> The scripts here are mocks (they just return values / reference their locals). Replace a
> script's body with real device calls — e.g. `robot.move(from_spot, to_spot)` in a
> transport, or an instrument read in `read` — to drive real hardware; the code may
> `import` anything the host Python can.
