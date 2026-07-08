from __future__ import annotations

from typing import Any

RLENS_MODES = (
    "opportunistic",
    "required",
    "strict",
    "live-first",
    "external-safe",
)
RLENS_CONTEXT_REQUIRED_MODES = {"required", "strict", "external-safe"}
RLENS_SKIP_REASONS = (
    "not_required",
    "live_first_primary",
    "context_pack_unavailable",
    "stale_context_explicitly_allowed",
    "external_safe_export_blocked",
    "trivial_task",
    "not_yet_generated",
    "policy_override",
)


def _object_field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _task_policy(task_raw: dict[str, Any]) -> dict[str, Any]:
    execution = task_raw.get("execution")
    metadata = task_raw.get("metadata")
    candidates = (
        task_raw.get("rlens_policy"),
        _object_field(execution, "rlens_policy"),
        _object_field(metadata, "rlens_policy"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return {"mode": "opportunistic"}


def task_rlens_context_ref(task_raw: dict[str, Any]) -> dict[str, Any] | None:
    execution = task_raw.get("execution")
    metadata = task_raw.get("metadata")
    candidates = (
        task_raw.get("rlens_context_ref"),
        _object_field(execution, "rlens_context_ref"),
        _object_field(metadata, "rlens_context_ref"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return None


def evaluate_task_rlens_policy(task_raw: dict[str, Any]) -> dict[str, Any]:
    policy = _task_policy(task_raw)
    mode = policy.get("mode", "opportunistic")
    if mode not in RLENS_MODES:
        # Schema validation should catch this for registry documents. Keep a
        # defensive value for callers that inspect unvalidated task objects.
        mode = "opportunistic"
    context_ref = task_rlens_context_ref(task_raw)
    skip_reason = policy.get("skip_reason")
    if not isinstance(skip_reason, str) or skip_reason not in RLENS_SKIP_REASONS:
        skip_reason = None
    task_profile = policy.get("task_profile")
    if not isinstance(task_profile, str) or not task_profile:
        task_profile = None
    requires_context = mode in RLENS_CONTEXT_REQUIRED_MODES
    result: dict[str, Any] = {
        "schema_version": 1,
        "mode": mode,
        "requires_context": requires_context,
        "status": "not_required",
        "task_profile": task_profile,
        "has_context_ref": context_ref is not None,
        "skip_reason": skip_reason,
        "block_reason": None,
        "does_not_establish": [
            "actual_agent_reading",
            "answer_correct",
            "repo_understood",
            "claims_true",
            "runtime_correctness",
        ],
    }
    if context_ref is not None:
        result["status"] = "satisfied"
        return result
    if skip_reason is not None:
        result["status"] = "skipped"
        return result
    if requires_context:
        result["status"] = "blocked"
        result["block_reason"] = f"rlens_policy_{mode}_requires_context_ref_or_skip_reason"
        return result
    if mode == "live-first":
        result["skip_reason"] = "live_first_primary"
    elif mode == "opportunistic":
        result["skip_reason"] = "not_required"
    return result


def rlens_policy_block_reason(task_raw: dict[str, Any]) -> str | None:
    evaluation = evaluate_task_rlens_policy(task_raw)
    block_reason = evaluation.get("block_reason")
    return block_reason if isinstance(block_reason, str) and block_reason else None
