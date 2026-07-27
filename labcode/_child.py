"""labcode's out-of-process child harness (the partial-output dialect).

Like ``ofplang.run.simulator._child``, but labcode process scripts follow the labcode
dialect's **partial** output convention (SPECIFICATIONS.md §1.2): a script returns only the
outputs it computes, and the backend fills the rest -- carrying an Object output through
``objects.map`` (identity preserved) and defaulting the others. So this child does **no**
output verification; it just returns the script's raw result mapping. The merge, the
conformance check, and the rejection of any *extra* (undeclared) output name all happen in
`labcode.backend.LabcodeBackend`.

A transport script is side-effect only (its return is ignored). Protocol (JSON on stdin,
outcome to ``result_path``) is identical to the upstream child; only the process branch
differs (raw, unverified, vs verified-exactly)."""

from __future__ import annotations

import json
import sys
import traceback

from ofplang.run.simulator import DeviceComputationError, run_python_script


def main() -> int:
    try:
        job = json.load(sys.stdin)
        result_path = job["result_path"]
    except Exception as exc:  # cannot even read the job -- a harness-level failure
        print(f"lc child: cannot read job: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    try:
        raw = run_python_script(job.get("code") or "", job.get("inputs") or {})
        if job.get("kind") == "transport":
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
