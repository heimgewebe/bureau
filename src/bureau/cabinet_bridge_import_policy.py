from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cabinet_bridge import CabinetBridgeError

POLICY_KIND = "bureau.cabinet_bridge_import_review_contract_policy"
REVIEW_KIND = "bureau.cabinet_bridge_import_review_contract_policy_review"
REQUIRED_RECEIPT = {
    "kind": "cabinet_bridge_review_receipt",
    "status": "review_recorded",
    "importAllowed": False,
    "importReviewRequired": True,
    "dispatchAllowed": False,
    "queueMutationAllowed": False,
    "taskCreationAllowed": False,
}
REQUIRED_INPUTS = {
    "current_cabinet_bridge_policy",
    "bridge_probe_report",
    "bridge_preview",
    "bridge_review_gate",
    "bridge_receipt",
    "cabinet_commit_sha",
    "bureau_commit_sha",
    "explicit_target_path",
    "explicit_write_surface",
    "non_ci_reviewer",
}
NON_EFFECTS = {
    "createTasks",
    "mutateQueues",
    "writeBureauRegistry",
    "dispatchWork",
    "runRuntimeActions",
}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CabinetBridgeError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CabinetBridgeError(f"{label} must be a list")
    return value


def _load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        return _object(json.loads(source.read_text(encoding="utf-8")), "policy")
    except FileNotFoundError as exc:
        raise CabinetBridgeError(f"policy missing: {source}") from exc
    except json.JSONDecodeError as exc:
        raise CabinetBridgeError(f"policy invalid JSON: {exc.msg}") from exc


def _resolve_document(policy_path: Path, document_value: Any) -> Path:
    if not isinstance(document_value, str) or not document_value.strip():
        raise CabinetBridgeError("policy document must be a non-empty string")
    document = Path(document_value)
    if document.is_absolute():
        return document
    candidate = policy_path.parent / document
    if candidate.exists():
        return candidate
    return document


def validate_policy(path: str | Path) -> dict[str, Any]:
    policy_path = Path(path)
    policy = _load_json(policy_path)

    if policy.get("schemaVersion") != 1:
        raise CabinetBridgeError("policy schemaVersion must be 1")
    if policy.get("kind") != POLICY_KIND:
        raise CabinetBridgeError(f"policy kind must be {POLICY_KIND!r}")
    if policy.get("version") != "0":
        raise CabinetBridgeError("policy version must be 0")

    document = _resolve_document(policy_path, policy.get("document"))
    if not document.exists():
        raise CabinetBridgeError(f"policy document missing: {document}")

    receipt = _object(policy.get("requiredReceipt"), "requiredReceipt")
    for key, expected in REQUIRED_RECEIPT.items():
        if receipt.get(key) != expected:
            raise CabinetBridgeError(f"requiredReceipt.{key} must be {expected!r}")

    inputs = _list(policy.get("requiredInputs"), "requiredInputs")
    input_values = {item for item in inputs if isinstance(item, str)}
    missing_inputs = sorted(REQUIRED_INPUTS - input_values)
    if missing_inputs:
        raise CabinetBridgeError("requiredInputs missing: " + ", ".join(missing_inputs))
    if len(input_values) != len(inputs):
        raise CabinetBridgeError("requiredInputs must contain unique strings")

    non_effects = _object(policy.get("nonEffects"), "nonEffects")
    missing_effects = sorted(NON_EFFECTS - set(non_effects))
    if missing_effects:
        raise CabinetBridgeError("nonEffects missing: " + ", ".join(missing_effects))
    for key in NON_EFFECTS:
        if non_effects.get(key) is not False:
            raise CabinetBridgeError(f"nonEffects.{key} must stay false")

    return {
        "schemaVersion": 1,
        "kind": REVIEW_KIND,
        "status": "valid",
        "policy": str(policy_path),
        "document": str(document),
        "importAllowed": receipt["importAllowed"],
        "importReviewRequired": receipt["importReviewRequired"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-cabinet-bridge-import-policy")
    result.add_argument(
        "--policy",
        default="docs/cabinet-bridge-import-review-contract-v0.policy.json",
    )
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        value = validate_policy(args.policy)
    except CabinetBridgeError as exc:
        print(f"bureau-cabinet-bridge-import-policy: {exc}", file=sys.stderr)
        return 2
    indent = 2 if args.json else None
    print(json.dumps(value, indent=indent, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
