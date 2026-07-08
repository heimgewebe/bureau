from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cabinet_bridge import EFFECT_FLAGS, CabinetBridgeError

CANDIDATE_KIND = "cabinet_frontier_candidate"
REPORT_KIND = "cabinet_frontier_reader_report"
PREVIEW_KIND = "cabinet_frontier_preview"
REVIEW_GATE_KIND = "cabinet_frontier_review_gate"
RECEIPT_KIND = "cabinet_frontier_review_receipt"
FORBIDDEN_EFFECTS = {
    "bureau_task_creation",
    "queue_mutation",
    "agent_dispatch",
    "merge_or_push",
    "runtime_mutation",
    "cleanup_action",
    "dump_generation",
    "authority_inference",
}
CANDIDATE_EFFECT_FLAGS = (
    "taskCreationAllowed",
    "queueMutationAllowed",
    "dispatchAllowed",
    "mergeOrPushAllowed",
    "runtimeMutationAllowed",
    "cleanupAllowed",
    "dumpGenerationAllowed",
    "authorityInferenceAllowed",
)
RECEIPT_DECISIONS = ("ready-for-design", "changes-requested", "rejected")


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


def _texts(values: list[str] | None, label: str) -> list[str]:
    if not values:
        raise CabinetBridgeError(f"{label} must contain at least one item")
    return [_text(value, label) for value in values]


def _effect_closure() -> dict[str, bool]:
    return {field: False for field in EFFECT_FLAGS}


def _require_false(container: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    for field in fields:
        if container.get(field) is not False:
            raise CabinetBridgeError(f"{label} must keep {field} false")


def _load_json(path: str | Path, label: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CabinetBridgeError(f"{label} missing: {source}") from exc
    except json.JSONDecodeError as exc:
        raise CabinetBridgeError(f"{label} invalid JSON: {exc.msg}") from exc
    return _object(value, label)


def _load_jsonl(path: str | Path, label: str) -> list[dict[str, Any]]:
    source = Path(path).expanduser()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CabinetBridgeError(f"{label} missing: {source}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CabinetBridgeError(f"{label} invalid JSONL line {line_no}: {exc.msg}") from exc
        rows.append(_object(value, f"{label} line {line_no}"))
    if not rows:
        raise CabinetBridgeError(f"{label} must contain at least one candidate")
    return rows


def _existing_frontier_sources(registry_root: str | Path | None) -> set[str]:
    if registry_root is None:
        return set()
    root = Path(registry_root).expanduser()
    tasks_dir = root / "registry/tasks"
    if not tasks_dir.exists():
        return set()
    result: set[str] = set()
    for task_path in sorted(tasks_dir.glob("*.json")):
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CabinetBridgeError(f"registry task source scan failed: {task_path}") from exc
        except json.JSONDecodeError as exc:
            raise CabinetBridgeError(
                f"registry task source scan invalid JSON: {task_path}"
            ) from exc
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        for key in ("source_frontier_candidate_id", "sourceCandidateId", "source_candidate_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                result.add(value)
    return result


def _candidate_reasons(candidate: dict[str, Any], *, existing_sources: set[str]) -> list[str]:
    reasons: list[str] = []
    if candidate.get("schemaVersion") != 1:
        reasons.append("invalid_schema_version")
    if candidate.get("kind") != CANDIDATE_KIND:
        reasons.append("invalid_kind")
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str) or not candidate_id.startswith("frontier:"):
        reasons.append("invalid_id")
    elif candidate_id in existing_sources:
        reasons.append("existing_task_source_collision")

    target = candidate.get("target") if isinstance(candidate.get("target"), dict) else None
    if not target:
        reasons.append("missing_target")
    else:
        repository = target.get("repository")
        organ = target.get("organ")
        if not isinstance(repository, str) or "/" not in repository:
            reasons.append("invalid_target_repository")
        if not isinstance(organ, str) or not organ:
            reasons.append("invalid_target_organ")

    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else None
    if not proposal:
        reasons.append("missing_proposal")
    else:
        risk = proposal.get("risk")
        if risk == "high":
            reasons.append("high_risk_requires_human_release")
        elif risk not in {"low", "medium", "unknown"}:
            reasons.append("invalid_risk")
        for field in ("title", "summary", "nextAction", "responsibleOrgan"):
            if not isinstance(proposal.get(field), str) or not proposal[field]:
                reasons.append(f"missing_proposal_{field}")

    evidence = candidate.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        reasons.append("missing_evidence")
    acceptance = candidate.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        reasons.append("missing_acceptance")

    forbidden = candidate.get("forbiddenEffects")
    if (
        not isinstance(forbidden, list)
        or set(forbidden) != FORBIDDEN_EFFECTS
        or len(forbidden) != len(FORBIDDEN_EFFECTS)
    ):
        reasons.append("forbidden_effects_not_exact")
    flags = candidate.get("effectFlags") if isinstance(candidate.get("effectFlags"), dict) else None
    if not flags or any(flags.get(field) is not False for field in CANDIDATE_EFFECT_FLAGS):
        reasons.append("effect_flags_not_false")
    return reasons


def read_frontier(
    frontier_path: str | Path,
    *,
    registry_root: str | Path | None = None,
) -> dict[str, Any]:
    candidates = _load_jsonl(frontier_path, "Cabinet Frontier")
    existing_sources = _existing_frontier_sources(registry_root)
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        reasons = _candidate_reasons(candidate, existing_sources=existing_sources)
        if candidate_id in seen:
            reasons.append("duplicate_frontier_id")
        if candidate_id:
            seen.add(candidate_id)
        proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
        target = candidate.get("target") if isinstance(candidate.get("target"), dict) else {}
        evidence_value = candidate.get("evidence")
        acceptance_value = candidate.get("acceptance")
        row = {
            "id": candidate_id,
            "decision": "blocked" if reasons else "admissible",
            "reasons": reasons,
            "targetRepository": target.get("repository"),
            "targetOrgan": target.get("organ"),
            "risk": proposal.get("risk"),
            "evidenceCount": len(evidence_value) if isinstance(evidence_value, list) else 0,
            "acceptanceCount": len(acceptance_value) if isinstance(acceptance_value, list) else 0,
            "candidate": candidate,
        }
        rows.append(row)
    admissible = sum(1 for row in rows if row["decision"] == "admissible")
    return {
        "schemaVersion": 1,
        "kind": REPORT_KIND,
        "mode": "read_only_frontier_reader",
        "frontierPath": str(Path(frontier_path).expanduser()),
        "registryRoot": str(Path(registry_root).expanduser()) if registry_root else None,
        **_effect_closure(),
        "candidateCount": len(rows),
        "admissibleCount": admissible,
        "blockedCount": len(rows) - admissible,
        "candidates": rows,
        "doesNotEstablish": [
            "task_approval",
            "merge_readiness",
            "runtime_correctness",
            "claim_truth",
            "autonomous_dispatch",
            "bureau_import_implemented",
            "bureau_task_created",
        ],
    }


def _load_report(path: str | Path) -> dict[str, Any]:
    report = _load_json(path, "frontier reader report")
    if report.get("schemaVersion") != 1:
        raise CabinetBridgeError("frontier reader report schemaVersion must be 1")
    if report.get("kind") != REPORT_KIND:
        raise CabinetBridgeError(f"frontier reader report kind must be {REPORT_KIND}")
    _require_false(report, EFFECT_FLAGS, "frontier reader report")
    _list(report.get("candidates"), "frontier reader report candidates")
    return report


def _admissible_row(report: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for raw in _list(report.get("candidates"), "frontier reader report candidates"):
        row = _object(raw, "frontier reader row")
        if row.get("id") != candidate_id:
            continue
        if row.get("decision") != "admissible":
            raise CabinetBridgeError(f"frontier candidate is not admissible: {candidate_id}")
        if row.get("reasons") not in ([], None):
            raise CabinetBridgeError(f"frontier candidate has blocking reasons: {candidate_id}")
        return row
    raise CabinetBridgeError(f"frontier candidate not found: {candidate_id}")


def preview_frontier_candidate(
    report_path: str | Path,
    *,
    candidate_id: str,
    approve: bool,
) -> dict[str, Any]:
    if not approve:
        raise CabinetBridgeError("frontier preview requires explicit --approve")
    candidate_id = _text(candidate_id, "candidate_id")
    row = _admissible_row(_load_report(report_path), candidate_id)
    candidate = _object(row.get("candidate"), "frontier candidate")
    proposal = _object(candidate.get("proposal"), "frontier proposal")
    return {
        "schemaVersion": 1,
        "kind": PREVIEW_KIND,
        "mode": "proposal_only",
        "approved": True,
        **_effect_closure(),
        "sourceCandidateId": candidate_id,
        "targetRepository": row.get("targetRepository"),
        "targetOrgan": row.get("targetOrgan"),
        "risk": row.get("risk"),
        "proposal": {
            "title": proposal.get("title"),
            "summary": proposal.get("summary"),
            "nextAction": proposal.get("nextAction"),
            "responsibleOrgan": proposal.get("responsibleOrgan"),
            "priorityHint": proposal.get("priorityHint"),
        },
        "acceptance": candidate.get("acceptance", []),
        "evidence": candidate.get("evidence", []),
        "metadata": {
            "source": "cabinet_frontier_reader",
            "source_frontier_candidate_id": candidate_id,
            "task_creation_allowed": False,
            "queue_mutation_allowed": False,
            "dispatch_allowed": False,
            "import_allowed": False,
        },
    }


def review_frontier_preview(path: str | Path) -> dict[str, Any]:
    preview = _load_json(path, "frontier preview")
    if preview.get("schemaVersion") != 1:
        raise CabinetBridgeError("frontier preview schemaVersion must be 1")
    if preview.get("kind") != PREVIEW_KIND:
        raise CabinetBridgeError(f"frontier preview kind must be {PREVIEW_KIND}")
    if preview.get("mode") != "proposal_only":
        raise CabinetBridgeError("frontier preview mode must be proposal_only")
    if preview.get("approved") is not True:
        raise CabinetBridgeError("frontier preview approved must be true")
    _require_false(preview, EFFECT_FLAGS, "frontier preview")
    candidate_id = _text(preview.get("sourceCandidateId"), "sourceCandidateId")
    if not _list(preview.get("evidence"), "frontier preview evidence"):
        raise CabinetBridgeError("frontier preview evidence must be non-empty")
    if not _list(preview.get("acceptance"), "frontier preview acceptance"):
        raise CabinetBridgeError("frontier preview acceptance must be non-empty")
    metadata = _object(preview.get("metadata"), "frontier preview metadata")
    metadata_flags = (
        "task_creation_allowed",
        "queue_mutation_allowed",
        "dispatch_allowed",
        "import_allowed",
    )
    for field in metadata_flags:
        if metadata.get(field) is not False:
            raise CabinetBridgeError(f"frontier preview metadata must keep {field} false")
    return {
        "schemaVersion": 1,
        "kind": REVIEW_GATE_KIND,
        "status": "requires_human_review",
        "reviewRequired": True,
        **_effect_closure(),
        "sourceCandidateId": candidate_id,
        "targetRepository": preview.get("targetRepository"),
        "targetOrgan": preview.get("targetOrgan"),
        "risk": preview.get("risk"),
    }


def create_frontier_receipt(
    review_gate_path: str | Path,
    *,
    reviewer: str,
    decision: str,
    evidence: list[str] | None,
    note: str | None = None,
) -> dict[str, Any]:
    if decision not in RECEIPT_DECISIONS:
        raise CabinetBridgeError("decision must be one of: " + ",".join(RECEIPT_DECISIONS))
    reviewer_value = _text(reviewer, "reviewer")
    evidence_values = _texts(evidence, "evidence")
    gate = _load_json(review_gate_path, "frontier review gate")
    if gate.get("schemaVersion") != 1:
        raise CabinetBridgeError("frontier review gate schemaVersion must be 1")
    if gate.get("kind") != REVIEW_GATE_KIND:
        raise CabinetBridgeError(f"frontier review gate kind must be {REVIEW_GATE_KIND}")
    if gate.get("status") != "requires_human_review":
        raise CabinetBridgeError("frontier review gate status must be requires_human_review")
    if gate.get("reviewRequired") is not True:
        raise CabinetBridgeError("frontier review gate must require review")
    _require_false(gate, EFFECT_FLAGS, "frontier review gate")
    receipt: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": RECEIPT_KIND,
        "status": "review_recorded",
        "decision": decision,
        "reviewer": reviewer_value,
        "evidence": evidence_values,
        "sourceGate": {
            "sourceCandidateId": _text(gate.get("sourceCandidateId"), "sourceCandidateId"),
            "status": gate["status"],
            "targetRepository": gate.get("targetRepository"),
            "targetOrgan": gate.get("targetOrgan"),
        },
        "importAllowed": False,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
    }
    if note:
        receipt["note"] = _text(note, "note")
    return receipt


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-cabinet-frontier-reader")
    sub = result.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read")
    read.add_argument("--frontier", required=True)
    read.add_argument("--registry-root")
    read.add_argument("--json", action="store_true")
    preview = sub.add_parser("preview")
    preview.add_argument("--report", required=True)
    preview.add_argument("--candidate-id", required=True)
    preview.add_argument("--approve", action="store_true")
    preview.add_argument("--json", action="store_true")
    review = sub.add_parser("review")
    review.add_argument("--preview", required=True)
    review.add_argument("--json", action="store_true")
    receipt = sub.add_parser("receipt")
    receipt.add_argument("--review-gate", required=True)
    receipt.add_argument("--reviewer", required=True)
    receipt.add_argument("--decision", required=True, choices=RECEIPT_DECISIONS)
    receipt.add_argument("--evidence", required=True, action="append")
    receipt.add_argument("--note")
    receipt.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "read":
            value = read_frontier(args.frontier, registry_root=args.registry_root)
        elif args.command == "preview":
            value = preview_frontier_candidate(
                args.report,
                candidate_id=args.candidate_id,
                approve=args.approve,
            )
        elif args.command == "review":
            value = review_frontier_preview(args.preview)
        else:
            value = create_frontier_receipt(
                args.review_gate,
                reviewer=args.reviewer,
                decision=args.decision,
                evidence=args.evidence,
                note=args.note,
            )
    except CabinetBridgeError as exc:
        print(f"bureau-cabinet-frontier-reader: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            value,
            indent=2 if getattr(args, "json", False) else None,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
