# Changelog

What changed in each release of labcode, for someone deciding whether to upgrade. The
specification the section numbers refer to is [`docs/SPECIFICATIONS.md`](docs/SPECIFICATIONS.md).

Releases before 0.9.0 are described in the messages of their `Release vX.Y.Z` commits
(`git log --grep '^Release v'`).

## 0.10.4 — 2026-10-10

No code changes; the siblings move.

### Added

- **A `branch` runs on a condition produced during the run.** A branch whose
  condition is made by an activity -- a script's verdict on a measured value, an
  instrument's flag -- runs the arm the value picks. The scheduler plans the branch on
  its `then` arm until the value exists and lets nothing of it start before then,
  marking the moment in the plan with a `decision` (ofplang-schedule SPEC §6.14); the
  run states the arm in the document's `expansion.arms` as soon as the value is
  recorded and replans (ofplang-schedule 0.16, ofplang-run 0.16). The arm not planned
  is checked without a solve, and a workflow whose other arm could never be planned is
  refused before anything runs (`arm_unplannable`). In 0.10.3 such a branch was refused
  (`branch_arm_unknown`).

### Known limitations

- **`lc export` cannot read a plan that waits on such a branch yet.** The export viewer
  does not know the `decision` entry, so a plan with one is refused; plans without one
  are unaffected. Planning and running are not.

Requires ofplang-schedule 0.16 and ofplang-run 0.16 (ofplang-validate 0.4.1 as before).

## 0.10.3 — 2026-10-08

No code changes; the siblings move.

### Added

- **A `branch` runs where the boundary decides its arm.** A branch whose condition is
  an entry input -- a flag, or one flag per element inside a `map` -- runs the arm the
  flag picks: the runner states each arm in the document's `expansion.arms`
  (ofplang-schedule SPEC §6.13) and the scheduler expands the branch with it
  (ofplang-schedule 0.15, ofplang-run 0.15). A flag left out is `false` and reported
  (`entry_input_defaulted`). A branch whose condition is produced during the run is
  refused before anything runs (`branch_arm_unknown`).

Requires ofplang-schedule 0.15 and ofplang-run 0.15 (ofplang-validate 0.4.1 as before).

## 0.10.2 — 2026-10-07

No code changes; the siblings move.

### Added

- **A `map` / `fold` over a list of values runs.** `lc run` is given a list of labels,
  volumes or the like and makes one invocation per element: the runner counts each
  list and states its length in the document's `expansion` (ofplang-schedule SPEC
  §6.13), and the scheduler expands by it (ofplang-schedule 0.14, ofplang-run 0.14).
  Until now such a workflow was refused (`array_length_unknown`).

### Changed — may need your attention

- **A list zipped with an Array of plates of another length is refused before the run
  starts** (`each_length_mismatch`), where it used to stop that job at its first check.
- **A structured node's `outputs` is checked as the specification says** (ofplang-validate
  0.4.1). An entry with no `mode` is `missing_required_key`, and a reference to an output
  the node does not expose -- one its `outputs` drops, or one dropped by default -- is
  `output_not_exposed`. Both passed validation before.

Requires ofplang-validate 0.4.1, ofplang-schedule 0.14.1 and ofplang-run 0.14.1.

## 0.10.1 — 2026-10-07

labcode follows specification revision 0.5. No code changes; the siblings move.

### Changed — may need your attention

- **A composite returns every output it declares.** An output port with no `returns`
  entry is now `output_not_returned`, and a `returns` entry naming no output port is
  `return_port_not_found` (ofplang-validate 0.4). Until now a Pure Data output could go
  unreturned.
- **The `scheduling` section is gone from the language.** It is now an unknown key, and
  `scheduling_policies` in `features` an unknown feature. Delete the section and the
  feature name; nothing else depended on them, and no plan used them.
- An Object that comes in at the boundary and is returned as it came is now planned and
  run, moved to the spot its output is bound to (ofplang-schedule 0.13.1, ofplang-run
  0.13.1).

Requires ofplang-validate 0.4, ofplang-schedule 0.13.1 and ofplang-run 0.13.1.

## 0.10.0 — 2026-10-05

labcode runs a protocol repeated over every plate (`map` / `fold`), and stops making up
a value a script did not compute.

### Added

- **`map` and `fold`** run, as ofplang-schedule 0.13 expands them and ofplang-run 0.13
  executes the expansion. An **Array of Objects** at the run boundary is one Object per
  element: its `view` is a list of views, one per spot, and each element gets its own
  `_id`, keyed by the element (`plates[1]`, §4.3). An invocation's node path carries its
  iteration index, and so do the keys of what it creates.
- **What a run made up, it says** (`lc run` prints each as `lc run: warning: ...`):
  - `entry_input_defaulted` — a boundary view written in part, or not at all: the
    declared fields it left out run on their type's default (§4.2). Once per Object.
  - `output_view_defaulted` — a script that did not return an Object it created: the
    view runs on its type's default (§1.2). Once per process and port.
  - `_id` is never reported; minting it is labcode's identity, not a default.

### Changed — may need your attention

- **A script must return every Pure Data output it declares** (§1.2). Until now one left
  out took its type's default — `return {}` from a `read` gave `od: 0.0`, a reading
  nothing took. It now fails the operation (`script_output_names`), as v0 §22.2 does. An
  Object output may still be left out: a mapped one is carried, a created one gets its
  default view (reported).
- **A process with no script anywhere that declares a Pure Data output is refused** by
  `lc run` before anything runs (§2): a no-op can compute nothing, so every run of it
  would fail. One with only Object outputs still runs as a no-op, with the warning it
  always had. A process with a script on some modes only fails if a mode without one is
  chosen.
- **A boundary view that is not a mapping** (or, for an Array, not a list of them) is
  refused. It used to be replaced by an empty view without a word.
- **An Object mapped from an input with no `_id`** fails the operation
  (`missing_object_id`). No run can reach this; it is there to catch a broken invariant
  early.
- A v0 §22 workflow script in a language other than Python fails the operation
  (`script_language`) instead of running as a no-op.
- Requires ofplang-validate 0.3, ofplang-schedule 0.13 and ofplang-run 0.13
  (specification revision 0.4).

`lc export` is unchanged; drawing a run that has `map` / `fold` invocations in it is not
yet supported there.

## 0.9.0 — 2026-09-28

labcode can now drive a lab over **LADS OPC UA** as well as SiLA2, and gains `lc export`.
Both are optional extras; nothing changes for an environment that uses neither.

### Added

- **`flavor: lads`** — a script that is the commands alone, handed its LADS OPC UA clients
  as a `sila2` script is handed SiLA2 ones (§1.10).
  - A connection may say `kind: lads`, and may name the `device` and functional `unit` on the
    server; left out, each is the only one there is (§1.5).
  - The script sees `lads_clients` / `lads_client`: one functional unit per machine, with the
    program runs, state-machine methods, cover movements and set-points LADS defines, each
    waiting for what it starts, and `.raw` for the asyncua client itself.
  - `labcode.lads_commands` holds the waits: a program run to its result, a state machine to
    settle (§1.10.1). A timeout fails the operation and aborts nothing, as SiLA2's does.
  - A script speaks one protocol. An operation may hold machines of both kinds, but a script
    commands only its own flavor's; reaching for the other kind says why
    (`lads_other_protocol` / `sila2_other_protocol`).
  - The client library is the `lads` extra (`asyncua`). labcode imports it only when a `lads`
    script connects.
- **`lc export`** — forwards to [ofplang-export](https://github.com/ofplang/export)'s CLI
  unchanged, as `lc validate` and `lc schedule` do. `lc export view plan.yaml -o plan.html`
  writes the workflow and its plan as one self-contained HTML file. It is the `export` extra;
  without it `lc export` names the extra and exits 2.
- **LADS examples** — `lads_seal.env.yaml` and `lads_plate_cycle.env.yaml` run the
  `sila2_seal` and `sila2_plate_cycle` workflows unchanged, with every machine reached over
  LADS. `run_all_lads_examples.py` runs both against the reference lab,
  [ofplang/mocklab](https://github.com/ofplang/mocklab).

### Changed

- `SPECIFICATIONS.md` moved to `docs/SPECIFICATIONS.md`.
- The `ofplang-schedule` cap is `<0.13`, admitting schedule 0.12. The floor is unchanged.
- The front door checks a connecting script's connection **per kind**. A `sila2` or `lads`
  script needs a machine of its own kind. An endpoint of the other kind draws a warning, as
  an endpoint with no connection does. Messages that named only `sila2` now name the flavor.
- What a connecting flavor needs (planning the clients, holding them for one operation,
  generating the wrapper) moved from `labcode.sila2` into `labcode.connections`. The `sila2`
  flavor's behaviour, messages and reason codes are unchanged.

### Fixed

- `examples/preflight_sila2_env.py` treated every declared connection as SiLA2. An
  environment with a LADS machine would have reported it as a failing SiLA2 server. It now
  checks SiLA2 machines only and lists the others as not checked.

### Documentation

- The reference lab is named by its current name, ofplang/mocklab (formerly
  ofplang-sila2-backend), and the examples record the commit they were last verified against.
- Section references written before probing took §1.6 pointed at the wrong sections and are
  corrected. The `labcode.sila2_commands` section is numbered §1.7.1.
- The README lists the optional extras, lists every example, and links by absolute URL, so the
  links also work on PyPI.
