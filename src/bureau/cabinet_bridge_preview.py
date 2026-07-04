from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cabinet_bridge import CabinetBridgeError


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CabinetBridgeError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CabinetBridgeError(f"{label} must be a list")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CabinetBridgeError(f"{label} must be a non-empty string")
    return value.strip()


def load_probe_report(path: str | Path) -> dict[str, Any]:
    probe_path = Path(path).expanduser()
    try:
        report = json.loads(probe_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CabinetBridgeError(f"probe report missing: {probe_path}") from exc
    except json.JSONDecodeError as exc:
        raise CabinetBridgeError(f"probe report invalid JSON: {exc.msg}") from exc
    report = _object(report, "probe report")
    if report.get("schemaVersion") != 1:
        raise CabinetBridgeError("probe report schemaVersion must be 1")
    if report.get("kind") != "cabinet_bureau_bridge_probe":
        raise CabinetBridgeError("probe report kind must be cabinet_bureau_bridge_probe")
    for field in ("dispatchAllowed", "queueMutationAllowed", "taskCreationAllowed"):
        if report.get(field) is not False:
            raise CabinetBridgeError(f"probe report must keep {field} false")
    _list(report.get("candidates"), "probe report candidates")
    return report


def _candidate(report: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for raw in _list(report.get("candidates"), "probe report candidates"):
        candidate = _object(raw, "probe report candidate")
        if candidate.get("id") != candidate_id:
            continue
        if candidate.get("decision") != "admissible":
            raise CabinetBridgeError(f"candidate is not admissible: {candidate_id}")
        if candidate.get("reasons") not in ([], None):
            raise CabinetBridgeError(f"candidate has blocking reasons: {candidate_id}")
        return candidate
    raise CabinetBridgeError(f"candidate not found in probe report: {candidate_id}")


def _resource(candidate: dict[str, Any]) -> str:
    raw = str(candidate.get("responsible_organ") or "unknown")
    safe = "".join(char.lower() if char.isalnum() else "." for char in raw).strip(".")
    return f"organ.{safe or 'unknown'}"


def preview_bridge_candidate(
    probe_report_path: str | Path,
    *,
    candidate_id: str,
    task_id: str,
    initiative: str,
    target_proof: str,
    approve: bool,
) -> dict[str, Any]:
    if not approve:
        raise CabinetBridgeError("preview requires explicit --approve")
    candidate_id = _text(candidate_id, "candidate_id")
    task_id = _text(task_id, "task_id")
    initiative = _text(initiative, "initiative")
    target_proof = _text(target_proof, "target_proof")
    candidate = _candidate(load_probe_report(probe_report_path), candidate_id)
    task = {
        "schema_version": 1,
        "id": task_id,
        "initiative": initiative,
        "title": f"Review Cabinet bridge candidate {candidate_id}",
        "state": "planned",
        "goal": f"Review Cabinet bridge candidate {candidate_id} before any import.",
        "required_capabilities": ["review"],
        "priority": {"lane": "next", "rank": 65},
        "execution": {"mode": "manual", "policy": "review-before-effect"},
        "claims": [{"resource": _resource(candidate), "mode": "read", "isolation": "none"}],
        "acceptance": [
            {"id": "target-proof", "assertion": target_proof},
            {"id": "no-auto-effect", "assertion": "Preview creates no operational effect."},
        ],
        "metadata": {
            "source": "cabinet_bridge_probe",
            "source_candidate_id": candidate_id,
            "source_candidate": candidate,
            "dispatch_allowed": False,
            "queue_mutation_allowed": False,
            "task_creation_allowed": False,
        },
    }
    return {
        "schemaVersion": 1,
        "kind": "cabinet_bridge_promotion_preview",
        "mode": "proposal_only",
        "approved": True,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "task": task,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-cabinet-bridge-preview")
    result.add_argument("--probe-report", required=True)
    result.add_argument("--candidate-id", required=True)
    result.add_argument("--task-id", required=True)
    result.add_argument("--initiative", required=True)
    result.add_argument("--target-proof", required=True)
    result.add_argument("--approve", action="store_true")
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        value = preview_bridge_candidate(
            args.probe_report,
            candidate_id=args.candidate_id,
            task_id=args.task_id,
            initiative=args.initiative,
            target_proof=args.target_proof,
            approve=args.approve,
        )
    except CabinetBridgeError as exc:
        print(f"bureau-cabinet-bridge-preview: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(value, indent=2 if args.json else None, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
