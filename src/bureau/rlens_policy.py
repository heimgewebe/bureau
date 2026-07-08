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
LIVE_FIRST_CLASSES = {"runtime", "deploy", "service", "incident", "ops"}
STRICT_CLASSES = {"pr", "review", "roadmap", "architecture", "security", "export"}
REQUIRED_CLASSES = {"repo", "code", "feature", "bugfix", "refactor", "registry", "docs"}


def _object_field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _task_policy_source(task_raw: dict[str, Any]) -> tuple[dict[str, Any], str]:
    execution = task_raw.get("execution")
    metadata = task_raw.get("metadata")
    candidates = (
        (task_raw.get("rlens_policy"), "task.rlens_policy"),
        (_object_field(execution, "rlens_policy"), "execution.rlens_policy"),
        (_object_field(metadata, "rlens_policy"), "metadata.rlens_policy"),
    )
    for candidate, source in candidates:
        if isinstance(candidate, dict):
            return candidate, source
    return {"mode": "opportunistic"}, "inferred"


def _task_policy(task_raw: dict[str, Any]) -> dict[str, Any]:
    policy, _source = _task_policy_source(task_raw)
    return policy


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
    """Return the enforcement policy used by dispatch/envelope code.

    This function is intentionally backward-compatible with the first rLens policy
    slice on origin/main: required/strict/external-safe block only when an explicit
    rlens_policy asks for context and no context ref or valid skip reason exists.
    """

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


def _explicit_task_class(task_raw: dict[str, Any], policy: dict[str, Any]) -> str | None:
    execution = task_raw.get("execution")
    metadata = task_raw.get("metadata")
    for value in (
        policy.get("task_class"),
        policy.get("task_profile"),
        _object_field(execution, "rlens_task_class"),
        _object_field(metadata, "rlens_task_class"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace("_", "-")
    return None


def infer_task_class(task_raw: dict[str, Any], policy: dict[str, Any] | None = None) -> str:
    policy = policy or {}
    explicit = _explicit_task_class(task_raw, policy)
    if explicit:
        return explicit
    claims = task_raw.get("claims") if isinstance(task_raw.get("claims"), list) else []
    text = " ".join(
        str(value).lower()
        for value in (
            task_raw.get("id", ""),
            task_raw.get("title", ""),
            task_raw.get("goal", ""),
            _object_field(task_raw.get("execution"), "mode") or "",
            " ".join(task_raw.get("required_capabilities", []) or []),
            " ".join(str(claim.get("resource", "")) for claim in claims if isinstance(claim, dict)),
        )
    )
    if any(token in text for token in ("deploy", "runtime", "service", "systemd", "ops")):
        return "runtime"
    if any(token in text for token in ("pr review", "pull request", "merge", "review")):
        return "pr"
    if any(token in text for token in ("roadmap", "architecture", "security", "export")):
        return "architecture"
    if any(token in text for token in ("repo.", "repository", "code", "refactor", "bugfix")):
        return "repo"
    return "unknown"


def infer_mode(task_raw: dict[str, Any], policy: dict[str, Any] | None = None) -> str:
    policy = policy or {}
    explicit = policy.get("mode")
    if isinstance(explicit, str) and explicit.strip():
        return explicit if explicit in RLENS_MODES else "opportunistic"
    task_class = infer_task_class(task_raw, policy)
    if task_class in LIVE_FIRST_CLASSES:
        return "live-first"
    if task_class in STRICT_CLASSES:
        return "strict"
    if task_class in REQUIRED_CLASSES:
        return "required"
    return "opportunistic"


def _requirement_for_mode(mode: str) -> str:
    return {
        "opportunistic": "optional",
        "required": "context_ref_or_explicit_skip_reason",
        "strict": "fresh_context_ref_or_explicit_skip_or_block_reason",
        "live-first": "live_tools_primary_rlens_optional",
        "external-safe": "context_pack_only_or_explicit_skip_reason",
    }.get(mode, "optional")


def evaluate_task_rlens_policy_report(task_raw: dict[str, Any]) -> dict[str, Any]:
    """Return an operator-facing policy report without widening enforcement.

    Legacy tasks without an explicit rlens_policy are reported as policy-missing
    when their inferred class would normally need rLens context. They are not
    strict blockers until the task adopts rlens_policy explicitly.
    """

    policy, source = _task_policy_source(task_raw)
    core = evaluate_task_rlens_policy(task_raw)
    explicit = source != "inferred"
    report_policy = policy if explicit else {}
    mode = core["mode"] if explicit else infer_mode(task_raw, report_policy)
    task_class = infer_task_class(task_raw, report_policy)
    context_present = task_rlens_context_ref(task_raw) is not None
    # ``evaluate_task_rlens_policy`` fills an internal non-blocking skip reason
    # such as ``not_required`` for inferred opportunistic legacy tasks. The
    # operator report must not present that as an explicit recorded skip.
    skip_present = (
        explicit
        and isinstance(core.get("skip_reason"), str)
        and bool(core["skip_reason"])
    )
    block_present = isinstance(core.get("block_reason"), str) and bool(core["block_reason"])
    reasons: list[str] = []

    if explicit:
        if core["status"] == "blocked":
            status = "block"
            reasons.append(str(core["block_reason"]))
        elif core["status"] == "skipped":
            status = "skip-recorded"
            reasons.append("explicit rlens skip reason recorded")
        elif core["status"] == "satisfied":
            status = "ok"
        elif mode == "live-first":
            status = "live-first"
            reasons.append("live-first task: rLens is optional repo/doc context")
        else:
            status = "not-required"
            reasons.append("rLens context is optional for this policy mode")
    elif mode in RLENS_CONTEXT_REQUIRED_MODES and not context_present:
        status = "policy-missing"
        reasons.append("inferred rLens mode is report-only until rlens_policy is explicit")
    elif mode == "live-first":
        status = "live-first"
        reasons.append("live-first task: rLens is optional repo/doc context")
    elif context_present:
        status = "ok"
    else:
        status = "not-required"
        reasons.append("opportunistic task: rLens context is optional")

    return {
        "task_id": str(task_raw.get("id", "")),
        "mode": mode,
        "task_class": task_class,
        "requirement": _requirement_for_mode(mode),
        "status": status,
        "context_ref_present": context_present,
        "skip_reason_present": skip_present,
        "block_reason_present": block_present,
        "reasons": reasons,
        "policy_source": source,
    }


def _raw_task(task: Any) -> dict[str, Any]:
    raw = getattr(task, "raw", task)
    return raw if isinstance(raw, dict) else {}


def evaluate_registry_rlens_policy(tasks: dict[str, Any]) -> dict[str, Any]:
    results = [evaluate_task_rlens_policy_report(_raw_task(task)) for task in tasks.values()]
    blockers = [item for item in results if item["status"] == "block"]
    return {
        "schema_version": 1,
        "kind": "bureau.rlens_task_policy_report",
        "modes": list(RLENS_MODES),
        "summary": {
            "tasks": len(results),
            "blockers": len(blockers),
            "policy_missing": sum(1 for item in results if item["status"] == "policy-missing"),
        },
        "blockers": blockers,
        "tasks": results,
        "does_not_establish": [
            "rlens_bundle_freshness",
            "repo_understanding",
            "task_correctness",
            "runtime_truth",
            "merge_readiness",
        ],
    }
