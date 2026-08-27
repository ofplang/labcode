"""labcode's out-of-process child harness (the partial-output dialect).

Like ``ofplang.run.simulator._child``, but labcode process scripts follow the labcode
dialect's **partial** output convention (SPECIFICATIONS.md §1.2): a script returns only the
outputs it computes, and the backend fills the rest -- carrying an Object output through
``objects.map`` (identity preserved) and defaulting the others. So this child does **no**
output verification; it just returns the script's raw result mapping. The merge, the
conformance check, and the rejection of any *extra* (undeclared) output name all happen in
`labcode.backend.LabcodeBackend`.

A transport or refill script is side-effect only (its return is ignored). Protocol (JSON on
stdin, outcome to ``result_path``) is identical to the upstream child; only the process
branch differs (raw, unverified, vs verified-exactly).

If the parent is recording the run, this process joins that record (`labcode.record`) and
what the script does to an instrument is measured where it happens. The record is finished
**after the outcome has been written**: finishing it can wait on something outside this
process, and an operation's outcome must not be the thing that waiting costs."""

from __future__ import annotations

import json
import sys
import traceback

from ofplang.run.simulator import DeviceComputationError, run_python_script

from labcode.record import child_recording


def main() -> int:
    try:
        job = json.load(sys.stdin)
        result_path = job["result_path"]
    except Exception as exc:  # cannot even read the job -- a harness-level failure
        print(f"lc child: cannot read job: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    # Reading the job has nothing to record; everything the operation does is in here.
    with child_recording():
        return _execute(job, result_path)


#: Kinds whose script acts rather than computes: the return is ignored, because there is
#: no output port to fill. Listed rather than "anything that is not a process", so a kind
#: added later has to say which it is instead of being asked for a mapping it was never
#: written to return -- which is how a refill script first failed here.
_SIDE_EFFECT_KINDS = frozenset({"transport", "replenishment"})


def _execute(job: dict, result_path: str) -> int:
    try:
        raw = run_python_script(job.get("code") or "", job.get("inputs") or {})
        if job.get("kind") in _SIDE_EFFECT_KINDS:
            payload: dict = {"outputs": {}}  # side-effect only; the return is ignored
        elif not isinstance(raw, dict):
            payload = {"error": {
                "code": "script_output_names",
                "message": f"script process {job.get('process')!r} returned "
                           f"{type(raw).__name__}, not a mapping",
            }}
        else:
            # A partial mapping is allowed: the backend merges and verifies it (§1.2).
            payload = {"outputs": raw}
    except DeviceComputationError as exc:
        payload = {"error": {"code": exc.code, "message": str(exc)}}
    except Exception as exc:  # a script error run_python_script did not wrap
        payload = {"error": {"code": "script_error", "message": f"{type(exc).__name__}: {exc}"}}

    try:
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:  # cannot deliver the result -- a harness-level failure
        print(f"lc child: cannot write result: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
