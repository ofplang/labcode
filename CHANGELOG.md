# Changelog

What changed in each release of labcode, for someone deciding whether to upgrade. The
specification the section numbers refer to is [`docs/SPECIFICATIONS.md`](docs/SPECIFICATIONS.md).

Releases before 0.9.0 are described in the messages of their `Release vX.Y.Z` commits
(`git log --grep '^Release v'`).

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
