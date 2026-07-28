"""labcode dialect front-door validation of the ``x-labcode`` extension (P5).

`ofplang-validate` checks a workflow is portable v0; `ofplang-schedule` merely tolerates
``x-`` extension keys in an environment (it neither interprets nor checks them). labcode
*owns* the ``x-labcode`` extension, so this module is its conformance validator -- the
reference implementation of the labcode dialect spec (`labcode/SPECIFICATIONS.md`). `lc
run` calls it at the front door, after the shared workflow front door, over the workflow
(needed for the (1)/(2) exclusivity rule) and the environment (where ``x-labcode`` lives).

Two kinds of check, matching the spec:

* **shape** -- an ``x-labcode.script`` must be a mapping with ``language: python`` and a
  string ``code`` (only python is supported).
* **relational / runnability** --
  - **(1)/(2) exclusive**: a process must not carry both a workflow ``script`` (1) and an
    env ``x-labcode.script`` (2); that is ambiguous.
  - **typed-default reachability** (a *warning*, not an error): a process with neither a
    workflow script nor any mode's ``x-labcode.script`` will run as a typed-default no-op
    (a device not yet scripted). Allowed -- convenient while mocking -- but surfaced.

Environment ``transports[]`` routes are checked the same way: an ``x-labcode.script`` is
shape-checked (python + string code); a real move (from != to) with no script is warned
(it runs as a bookkeeping-only no-op move, no device command).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from labcode.objectid import RESERVED_ID, reserved_collisions


@dataclass
class DialectResult:
    """Outcome of `validate_dialect`: ``ok`` iff there are no errors. ``errors`` block the
    run (a usage error); ``warnings`` are surfaced but do not block."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _mode_label(process: str, mode) -> str:
    return f"process {process!r} mode {mode!r}"


def validate_dialect(workflow: dict, environment: dict) -> DialectResult:
    """Validate the labcode ``x-labcode`` extension (P5) over `workflow` and
    `environment`. Returns a `DialectResult` (errors + warnings); the caller reports it
    and maps a non-ok result to a usage error."""
    errors: list[str] = []
    warnings: list[str] = []
    wf_procs = workflow.get("processes") or {}
    env_procs = environment.get("processes") or {}

    # Reserved Object identity: labcode injects `_id` into every Object type's view (an
    # implicit, value-layer identity). A user type that already declares `_id` collides
    # with that -- reject it rather than clobber the user's field.
    for type_name in reserved_collisions(workflow):
        errors.append(
            f"type {type_name!r} declares the reserved view field {RESERVED_ID!r}; "
            f"labcode owns it as an implicit Object identity and it must not be declared"
        )

    for name, eproc in env_procs.items():
        if not isinstance(eproc, dict):
            continue
        # (1) the workflow's own script process for this name (if any).
        wf_has_script = isinstance((wf_procs.get(name) or {}).get("script"), dict)
        # (2) modes carrying an env x-labcode.script (collected while shape-checking).
        modes_with_script: list = []

        for mode in eproc.get("modes") or []:
            if not isinstance(mode, dict):
                continue
            xlab = mode.get("x-labcode")
            if xlab is None:
                continue
            label = _mode_label(name, mode.get("id"))
            if not isinstance(xlab, dict):
                errors.append(f"{label}: x-labcode must be a mapping")
                continue
            script = xlab.get("script")
            if script is None:
                continue  # an x-labcode with no script (e.g. future keys); nothing to check
            if not isinstance(script, dict):
                errors.append(f"{label}: x-labcode.script must be a mapping")
                continue
            language = script.get("language")
            if language != "python":
                errors.append(
                    f"{label}: x-labcode.script.language must be 'python' (got {language!r})"
                )
            if not isinstance(script.get("code"), str):
                errors.append(f"{label}: x-labcode.script.code must be a string")
            modes_with_script.append(mode.get("id"))

        # (1)/(2) exclusivity.
        if wf_has_script and modes_with_script:
            errors.append(
                f"process {name!r} has both a workflow script (1) and an env "
                f"x-labcode.script (2); they are mutually exclusive"
            )
        # typed-default reachability -- a warning, not an error.
        if not wf_has_script and not modes_with_script:
            warnings.append(
                f"process {name!r} has no script (workflow or x-labcode); its operations "
                f"will run as a typed-default no-op"
            )

    _validate_transports(environment, errors, warnings)
    return DialectResult(ok=not errors, errors=errors, warnings=warnings)


def _transport_label(transport: dict) -> str:
    return (
        f"transport {transport.get('transporter')!r} "
        f"{transport.get('from')} -> {transport.get('to')}"
    )


def _validate_transports(environment: dict, errors: list, warnings: list) -> None:
    """Validate the ``x-labcode.script`` on each environment ``transports[]`` route (a
    transport script is side-effect only -- no ports, no outputs -- so only its shape is
    checked). A real move (from != to) with no script runs as a bookkeeping-only no-op
    move (no device command), which is warned."""
    for transport in environment.get("transports") or []:
        if not isinstance(transport, dict):
            continue
        label = _transport_label(transport)
        xlab = transport.get("x-labcode")
        if xlab is None:
            if transport.get("from") != transport.get("to"):  # a real move
                warnings.append(f"{label}: no x-labcode.script; it will run as a no-op move")
            continue
        if not isinstance(xlab, dict):
            errors.append(f"{label}: x-labcode must be a mapping")
            continue
        script = xlab.get("script")
        if script is None:
            continue
        if not isinstance(script, dict):
            errors.append(f"{label}: x-labcode.script must be a mapping")
            continue
        if script.get("language") != "python":
            errors.append(
                f"{label}: x-labcode.script.language must be 'python' "
                f"(got {script.get('language')!r})"
            )
        if not isinstance(script.get("code"), str):
            errors.append(f"{label}: x-labcode.script.code must be a string")
