from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cabinet_bridge import CabinetBridgeError

DECISIONS = ("ready-for-design", "changes-requested", "rejected")
EFFECT_FLAGS = (
    "importAllowed",
    "dispatchAllowed",
    "queueMutationAllowed",
    "taskCreationAllowed",
)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CabinetBridgeError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CabinetBridgeError(f"{label} must be a non-empty string")
    return value.strip()


def _texts(values: list[str] | None, label: str) -> list[str]:
    if not values:
        raise CabinetBridgeError(f"{label} must contain at least one item")
    return [_text(value, label) for value in values]


def _load_json(path: str | Path, label: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CabinetBridgeError(f"{label} missing: {source}") from exc
    except json.JSONDecodeError as exc:
        raise CabinetBridgeError(f"{label} invalid JSON: {exc.msg}") from exc
    return _object(value, label)


def _require_false(container: dict[str, Any], label: str) -> None:
    for field in EFFECT_FLAGS:
        if container.get(field) is not False:
            raise CabinetBridgeError(f"{label} must keep {field} false")


def load_review_gate(path: str | Path) -> dict[str, Any]:
    gate = _load_json(path, "bridge review gate")
    if gate.get("schemaVersion") != 1:
        raise CabinetBridgeError("review gate schemaVersion must be 1")
    if gate.get("kind") != "cabinet_bridge_preview_review_gate":
        raise CabinetBridgeError("review gate kind must be cabinet_bridge_preview_review_gate")
    if gate.get("status") != "requires_human_review":
        raise CabinetBridgeError("review gate status must be requires_human_review")
    if gate.get("reviewRequired") is not True:
        raise CabinetBridgeError("review gate must require review")
    _require_false(gate, "review gate")
    _text(gate.get("taskId"), "review gate taskId")
    _text(gate.get("sourceCandidateId"), "review gate sourceCandidateId")
    return gate


def create_review_receipt(
    review_gate_path: str | Path,
    *,
    reviewer: str,
    decision: str,
    evidence: list[str] | None,
    note: str | None = None,
) -> dict[str, Any]:
    reviewer_value = _text(reviewer, "reviewer")
    if decision not in DECISIONS:
        raise CabinetBridgeError("decision must be one of: " + ",".join(DECISIONS))
    evidence_values = _texts(evidence, "evidence")
    note_value = _text(note, "note") if note else None
    gate = load_review_gate(review_gate_path)
    receipt: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "cabinet_bridge_review_receipt",
        "status": "review_recorded",
        "decision": decision,
        "reviewer": reviewer_value,
        "evidence": evidence_values,
        "sourceGate": {
            "taskId": gate["taskId"],
            "sourceCandidateId": gate["sourceCandidateId"],
            "status": gate["status"],
        },
        "importAllowed": False,
        "importReviewRequired": True,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
    }
    if note_value is not None:
        receipt["note"] = note_value
    return receipt


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-cabinet-bridge-receipt")
    result.add_argument("--review-gate", required=True)
    result.add_argument("--reviewer", required=True)
    result.add_argument("--decision", required=True, choices=DECISIONS)
    result.add_argument("--evidence", required=True, action="append")
    result.add_argument("--note")
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        value = create_review_receipt(
            args.review_gate,
            reviewer=args.reviewer,
            decision=args.decision,
            evidence=args.evidence,
            note=args.note,
        )
    except CabinetBridgeError as exc:
        print(f"bureau-cabinet-bridge-receipt: {exc}", file=sys.stderr)
        return 2
    indent = 2 if args.json else None
    print(json.dumps(value, indent=indent, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
