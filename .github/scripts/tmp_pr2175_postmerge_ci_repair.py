from __future__ import annotations

import json
from pathlib import Path

TASK = Path(
    "registry/tasks/"
    "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-UNUSED-SUCCESSOR-20260827.json"
)
TEST = Path("tests/test_runtime_refresh.py")
MODE = "single-use-target-bound-source-precondition-v1"
VERIFIER = "runtime-refresh-no-run-evidence-v1"
KIND = "bureau_runtime_refresh_no_run_acceptance_contract"

spec = json.loads(TASK.read_text(encoding="utf-8"))
authority = spec["metadata"]["runtime_refresh_authority"]
if authority.get("mode") != "single-use-target-bound":
    raise SystemExit(f"unexpected authority mode: {authority.get('mode')!r}")
if not isinstance(authority.get("source_precondition"), dict):
    raise SystemExit("source_precondition missing")
if "no_run_closeout_acceptance" in authority:
    raise SystemExit("no_run_closeout_acceptance already present")

acceptance_ids = {item["id"] for item in spec["acceptance"]}
expected_ids = {
    "fresh-current-main",
    "source-ancestry-precondition",
    "single-runtime-effect",
    "immutable-runtime-readback",
    "runtime-only-scope",
}
if acceptance_ids != expected_ids:
    raise SystemExit(
        f"acceptance drift: expected {sorted(expected_ids)}, got {sorted(acceptance_ids)}"
    )

authority["mode"] = MODE
authority["no_run_closeout_acceptance"] = {
    "schema_version": 1,
    "kind": KIND,
    "criteria": {
        "fresh-current-main": {
            "verifier": VERIFIER,
            "required_evidence": ["approval-intent", "runtime-result"],
        },
        "source-ancestry-precondition": {
            "verifier": VERIFIER,
            "required_evidence": [
                "approval-intent",
                "runtime-result",
                "source-precondition",
            ],
        },
        "single-runtime-effect": {
            "verifier": VERIFIER,
            "required_evidence": [
                "runtime-result",
                "single-use-history",
                "run-lifecycle",
            ],
        },
        "immutable-runtime-readback": {
            "verifier": VERIFIER,
            "required_evidence": [
                "runtime-result",
                "immutable-readback",
                "state-store-integrity",
            ],
        },
        "runtime-only-scope": {
            "verifier": VERIFIER,
            "required_evidence": [
                "runtime-result",
                "single-use-history",
                "lease-release",
                "run-lifecycle",
            ],
        },
    },
}
TASK.write_text(
    json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)

text = TEST.read_text(encoding="utf-8")
old = "    assert len(paths) == 6\n"
new = "    assert len(paths) == 7\n"
if text.count(old) != 1:
    raise SystemExit(f"test cardinality anchor drift: {text.count(old)} matches")
TEST.write_text(text.replace(old, new, 1), encoding="utf-8")
