from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_BRIDGE_POLICY_PATH = Path.home() / "repos/cabinet/registry/ecosystem/bureau-bridge.json"
BRIDGE_DIRECTION = "cabinet_to_bureau_read_only_candidate_signal"


class CabinetBridgeError(RuntimeError):
    """Raised when a Cabinet-to-Bureau bridge input cannot be read safely."""


def _expect_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CabinetBridgeError(f"{label} must be an object")
    return value


def _expect_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CabinetBridgeError(f"{label} must be a list")
    return value


def _expect_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CabinetBridgeError(f"{label} must be a non-empty string")
    return value


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CabinetBridgeError(f"{label} missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CabinetBridgeError(f"{label} invalid JSON: {exc.msg}") from exc
    return _expect_dict(value, label)


def _load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CabinetBridgeError(f"{label} missing: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CabinetBridgeError(f"{label} invalid JSONL line {line_no}: {exc.msg}") from exc
        rows.append(_expect_dict(value, f"{label} line {line_no}"))
    return rows


def _cabinet_root_for_bridge_policy(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.parent.name == "ecosystem" and resolved.parent.parent.name == "registry":
        return resolved.parent.parent.parent
    return resolved.parent


def _resolve_under_root(root: Path, raw_path: str, label: str) -> Path:
    candidate = (root / raw_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CabinetBridgeError(f"{label} escapes Cabinet root: {raw_path}") from exc
    if not candidate.exists():
        raise CabinetBridgeError(f"{label} references missing path: {raw_path}")
    return candidate


def load_bridge_policy(path: str | Path = DEFAULT_BRIDGE_POLICY_PATH) -> dict[str, Any]:
    policy = _load_json(Path(path).expanduser(), "Cabinet-Bureau bridge policy")
    if policy.get("schema_version") != 1:
        raise CabinetBridgeError("bridge policy schema_version must be 1")
    if policy.get("direction") != BRIDGE_DIRECTION:
        raise CabinetBridgeError("bridge policy direction is invalid")
    _expect_string(policy.get("source_owner"), "bridge policy source_owner")
    _expect_string(policy.get("target_consumer"), "bridge policy target_consumer")
    _expect_list(policy.get("admissible_candidate_statuses"), "admissible_candidate_statuses")
    _expect_list(policy.get("blocked_statuses"), "blocked_statuses")
    _expect_list(policy.get("required_candidate_fields"), "required_candidate_fields")
    _expect_list(policy.get("prohibited_effects"), "prohibited_effects")
    _expect_list(policy.get("does_not_establish"), "does_not_establish")
    _expect_dict(policy.get("organ_roles"), "organ_roles")
    return policy


def _claim_source(policy: dict[str, Any], root: Path) -> Path:
    allowed_sources = _expect_list(policy.get("allowed_sources"), "allowed_sources")
    for raw_source in allowed_sources:
        if raw_source == "registry/ecosystem/claims.jsonl":
            return _resolve_under_root(root, raw_source, "allowed source")
    raise CabinetBridgeError("bridge policy does not expose registry/ecosystem/claims.jsonl")


def _field_present(claim: dict[str, Any], field: str) -> bool:
    if field == "expires_at_or_refresh_hint":
        return bool(claim.get("expires_at") or claim.get("refresh_hint"))
    value = claim.get(field)
    if field == "evidence":
        return isinstance(value, list) and bool(value)
    return isinstance(value, str) and bool(value)


def _is_expired(claim: dict[str, Any], today: date) -> bool:
    raw = claim.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return date.fromisoformat(raw) < today
    except ValueError:
        return True


def _candidate_decision(
    claim: dict[str, Any],
    *,
    admissible_statuses: set[str],
    blocked_statuses: set[str],
    required_fields: set[str],
    today: date,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    status = str(claim.get("status", ""))
    if status in blocked_statuses:
        reasons.append(f"blocked_status:{status}")
    if status not in admissible_statuses:
        reasons.append(f"not_admissible_status:{status or '<missing>'}")
    missing = sorted(field for field in required_fields if not _field_present(claim, field))
    if missing:
        reasons.append("missing_fields:" + ",".join(missing))
    if _is_expired(claim, today):
        reasons.append("expired")
    return ("blocked", reasons) if reasons else ("admissible", [])


def bridge_probe(
    bridge_policy_path: str | Path = DEFAULT_BRIDGE_POLICY_PATH,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Classify Cabinet bridge candidates without creating Bureau tasks.

    The probe is deliberately read-only. It loads the bridge policy and Cabinet
    claims, then reports which claim-shaped candidates are admissible under the
    policy. It never writes the Bureau registry, creates tasks, touches runtime
    state or dispatches Grabowski.
    """
    policy_path = Path(bridge_policy_path).expanduser()
    policy = load_bridge_policy(policy_path)
    root = _cabinet_root_for_bridge_policy(policy_path)
    claims_path = _claim_source(policy, root)
    claims = _load_jsonl(claims_path, "Cabinet bridge claims")

    admissible_statuses = {str(item) for item in policy["admissible_candidate_statuses"]}
    blocked_statuses = {str(item) for item in policy["blocked_statuses"]}
    required_fields = {str(item) for item in policy["required_candidate_fields"]}
    today_value = today or date.today()

    candidates: list[dict[str, Any]] = []
    for claim in claims:
        decision, reasons = _candidate_decision(
            claim,
            admissible_statuses=admissible_statuses,
            blocked_statuses=blocked_statuses,
            required_fields=required_fields,
            today=today_value,
        )
        candidates.append(
            {
                "id": str(claim.get("id", "")),
                "status": str(claim.get("status", "")),
                "decision": decision,
                "reasons": reasons,
                "evidence": claim.get("evidence", []),
                "expires_at": claim.get("expires_at"),
                "next_action": claim.get("next_action"),
                "responsible_organ": claim.get("responsible_organ"),
            }
        )

    admissible_count = sum(1 for candidate in candidates if candidate["decision"] == "admissible")
    return {
        "schemaVersion": 1,
        "kind": "cabinet_bureau_bridge_probe",
        "mode": "read_only",
        "bridgePolicyPath": str(policy_path),
        "claimsPath": str(claims_path),
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "candidateCount": len(candidates),
        "admissibleCount": admissible_count,
        "blockedCount": len(candidates) - admissible_count,
        "candidates": candidates,
        "doesNotEstablish": policy.get("does_not_establish", []),
    }
