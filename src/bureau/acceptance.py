from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
EVIDENCE_KIND = "bureau.acceptance_evidence"
EVALUATION_KIND = "bureau.acceptance_evaluation"
PASSED = "passed"
FAILED = "failed"
UNKNOWN = "unknown"
STATES = {PASSED, FAILED, UNKNOWN}

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Each verifier is a bounded contract. A criterion selects one by name through
# ``verifier`` and must use object evidence. The contract then fixes the
# authority class, revision binding and maximum evidence age; caller-provided
# evidence cannot weaken those boundaries.
VERIFIER_CONTRACTS: dict[str, dict[str, Any]] = {
    "code_merged": {
        "domain": "product",
        "authorities": ("github",),
        "max_age_seconds": 86_400,
        "revision_binding": ("task_sha256", "plan_sha256", "head_sha", "merge_commit_sha"),
    },
    "required_ci_green": {
        "domain": "product",
        "authorities": ("github",),
        "max_age_seconds": 21_600,
        "revision_binding": ("task_sha256", "plan_sha256", "head_sha"),
    },
    "deployment_complete": {
        "domain": "deployment",
        "authorities": ("grabowski", "target-runtime"),
        "max_age_seconds": 86_400,
        "revision_binding": ("task_sha256", "plan_sha256", "deployment_revision"),
    },
    "runtime_commit_contains": {
        "domain": "runtime",
        "authorities": ("grabowski", "target-runtime"),
        "max_age_seconds": 3_600,
        "revision_binding": (
            "task_sha256",
            "plan_sha256",
            "required_commit",
            "observed_commit",
        ),
    },
    "live_probe_passed": {
        "domain": "runtime",
        "authorities": ("grabowski", "target-runtime"),
        "max_age_seconds": 900,
        "revision_binding": ("task_sha256", "plan_sha256", "runtime_revision"),
    },
    "manual_observation": {
        "domain": "observation",
        "authorities": ("manual",),
        "max_age_seconds": 86_400,
        "revision_binding": ("task_sha256", "plan_sha256"),
    },
    "duration_soak_completed": {
        "domain": "observation",
        "authorities": ("grabowski", "target-runtime", "manual"),
        "max_age_seconds": 86_400,
        "revision_binding": ("task_sha256", "plan_sha256"),
    },
    "no_effect_verified": {
        "domain": "observation",
        "authorities": ("bureau", "grabowski"),
        "max_age_seconds": 3_600,
        "revision_binding": ("task_sha256", "plan_sha256", "scope_sha256"),
    },
    "artifact_hash_matches": {
        "domain": "product",
        "authorities": ("artifact-store", "bureau", "grabowski"),
        "max_age_seconds": 86_400,
        "revision_binding": ("task_sha256", "plan_sha256", "artifact_sha256"),
    },
}

_PASS_CHECK_STATES = {"success", "passed", "neutral", "skipped"}
_FAIL_CHECK_STATES = {"failure", "failed", "error", "cancelled", "timed_out"}
_PENDING_CHECK_STATES = {"pending", "queued", "in_progress", "waiting", "requested"}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _now(value: str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError("now must be an ISO-8601 timestamp with timezone")
    return parsed


def _sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def criterion_contract(criterion: Mapping[str, Any]) -> dict[str, Any] | None:
    verifier = criterion.get("verifier")
    if not isinstance(verifier, str):
        return None
    contract = VERIFIER_CONTRACTS.get(verifier)
    if contract is None or criterion.get("evidence_type") != "object":
        return None
    return {
        "verifier": verifier,
        "evidence_type": "object",
        **contract,
    }


def typed_criterion_contracts(criteria: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    invalid: list[str] = []
    for criterion in criteria:
        criterion_id = criterion.get("id")
        if not isinstance(criterion_id, str) or not criterion_id:
            invalid.append("<missing-id>")
            continue
        contract = criterion_contract(criterion)
        if contract is None:
            invalid.append(criterion_id)
            continue
        items.append({"criterion_id": criterion_id, **contract})
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": not invalid,
        "criteria": items,
        "invalid_criterion_ids": invalid,
    }


def _unknown(
    criterion_id: str, verifier: str | None, reason: str, domain: str | None
) -> dict[str, Any]:
    return {
        "criterion_id": criterion_id,
        "verifier": verifier,
        "domain": domain,
        "state": UNKNOWN,
        "reason": reason,
    }


def _fact_state(verifier: str, facts: Mapping[str, Any]) -> tuple[str, str]:
    if verifier == "code_merged":
        merged = facts.get("merged")
        if merged is True and _sha(facts.get("head_sha")) and _sha(facts.get("merge_commit_sha")):
            return PASSED, "merged-head-observed"
        if merged is False:
            return FAILED, "merge-observed-not-complete"
        return UNKNOWN, "merge-facts-incomplete"

    if verifier == "required_ci_green":
        checks = facts.get("checks")
        required_checks = facts.get("required_checks")
        if facts.get("complete") is not True:
            return UNKNOWN, "required-check-observation-incomplete"
        if not isinstance(required_checks, list) or not required_checks:
            return UNKNOWN, "required-check-set-unavailable"
        if any(not isinstance(name, str) or not name.strip() for name in required_checks):
            return UNKNOWN, "required-check-set-invalid"
        if len(set(required_checks)) != len(required_checks):
            return UNKNOWN, "required-check-set-duplicated"
        if not isinstance(checks, list) or not checks:
            return UNKNOWN, "required-checks-unavailable"
        observed: dict[str, str] = {}
        for check in checks:
            if not isinstance(check, Mapping):
                return UNKNOWN, "required-check-row-invalid"
            name = check.get("name")
            state = check.get("state")
            if not isinstance(name, str) or not name or not isinstance(state, str):
                return UNKNOWN, "required-check-row-incomplete"
            lowered = state.lower()
            if name in observed and observed[name] != lowered:
                return UNKNOWN, "required-check-row-conflict"
            observed[name] = lowered
        missing = [name for name in required_checks if name not in observed]
        if missing:
            return UNKNOWN, "required-check-missing"
        required_states = [observed[name] for name in required_checks]
        if any(state in _FAIL_CHECK_STATES for state in required_states):
            return FAILED, "required-check-failed"
        if any(
            state in _PENDING_CHECK_STATES or state not in _PASS_CHECK_STATES
            for state in required_states
        ):
            return UNKNOWN, "required-check-not-terminal-green"
        return PASSED, "required-checks-green"

    if verifier == "deployment_complete":
        if facts.get("failed") is True:
            return FAILED, "deployment-observed-failed"
        if facts.get("completed") is True and _sha256(facts.get("receipt_sha256")):
            return PASSED, "deployment-receipt-complete"
        return UNKNOWN, "deployment-not-completely-observed"

    if verifier == "runtime_commit_contains":
        contains = facts.get("contains_required_commit")
        if contains is True:
            return PASSED, "runtime-contains-required-commit"
        if contains is False:
            return FAILED, "runtime-does-not-contain-required-commit"
        return UNKNOWN, "runtime-containment-unknown"

    if verifier == "live_probe_passed":
        result = facts.get("result")
        if isinstance(result, str):
            lowered = result.lower()
            if lowered in {"pass", "passed", "success"}:
                return PASSED, "live-probe-passed"
            if lowered in {"fail", "failed", "failure", "error"}:
                return FAILED, "live-probe-failed"
        return UNKNOWN, "live-probe-unknown"

    if verifier == "manual_observation":
        accepted = facts.get("accepted")
        observer = facts.get("observer")
        observation = facts.get("observation")
        if (
            not isinstance(observer, str)
            or not observer
            or not isinstance(observation, str)
            or not observation
        ):
            return UNKNOWN, "manual-observation-incomplete"
        if accepted is True:
            return PASSED, "manual-observation-accepted"
        if accepted is False:
            return FAILED, "manual-observation-rejected"
        return UNKNOWN, "manual-observation-undecided"

    if verifier == "duration_soak_completed":
        required = facts.get("required_seconds")
        observed = facts.get("observed_seconds")
        if (
            not isinstance(required, int | float)
            or isinstance(required, bool)
            or not math.isfinite(float(required))
            or required < 0
        ):
            return UNKNOWN, "soak-requirement-invalid"
        if (
            not isinstance(observed, int | float)
            or isinstance(observed, bool)
            or not math.isfinite(float(observed))
            or observed < 0
        ):
            return UNKNOWN, "soak-observation-invalid"
        if facts.get("failed") is True:
            return FAILED, "soak-observed-failed"
        if facts.get("completed") is not True:
            return UNKNOWN, "soak-still-running"
        if observed < required:
            return FAILED, "soak-duration-too-short"
        return PASSED, "soak-duration-satisfied"

    if verifier == "no_effect_verified":
        count = facts.get("effect_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return UNKNOWN, "effect-count-unavailable"
        if count == 0:
            return PASSED, "no-effect-observed"
        return FAILED, "unexpected-effect-observed"

    if verifier == "artifact_hash_matches":
        expected = facts.get("expected_sha256")
        observed = facts.get("observed_sha256")
        if not _sha256(expected) or not _sha256(observed):
            return UNKNOWN, "artifact-hash-unavailable"
        if expected == observed:
            return PASSED, "artifact-hash-matches"
        return FAILED, "artifact-hash-mismatch"

    return UNKNOWN, "unsupported-verifier"


def _revision_valid(
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    task_sha256: str,
    plan_sha256: str | None,
) -> tuple[bool, str]:
    revision = evidence.get("revision")
    if not isinstance(revision, Mapping):
        return False, "revision-binding-missing"
    if revision.get("task_sha256") != task_sha256 or not _sha256(task_sha256):
        return False, "task-revision-mismatch"
    if plan_sha256 is not None and (
        revision.get("plan_sha256") != plan_sha256 or not _sha256(plan_sha256)
    ):
        return False, "plan-revision-mismatch"
    required = contract.get("revision_binding")
    if not isinstance(required, tuple):
        return False, "verifier-revision-contract-invalid"
    for field in required:
        if field == "plan_sha256" and plan_sha256 is None:
            continue
        value = revision.get(field)
        if not isinstance(value, str) or not value:
            return False, f"revision-field-missing:{field}"
        if field.endswith("_sha256") and not _sha256(value):
            return False, f"revision-field-invalid:{field}"
        if field in {
            "head_sha",
            "merge_commit_sha",
            "required_commit",
            "observed_commit",
        } and not _sha(value):
            return False, f"revision-field-invalid:{field}"
    return True, "revision-bound"


def _facts_match_revision(
    verifier: str, revision: Mapping[str, Any], facts: Mapping[str, Any]
) -> tuple[bool, str]:
    bindings: dict[str, tuple[tuple[str, str], ...]] = {
        "code_merged": (("head_sha", "head_sha"), ("merge_commit_sha", "merge_commit_sha")),
        "required_ci_green": (("head_sha", "head_sha"),),
        "deployment_complete": (("deployment_revision", "deployment_revision"),),
        "runtime_commit_contains": (
            ("required_commit", "required_commit"),
            ("observed_commit", "observed_commit"),
        ),
        "live_probe_passed": (("runtime_revision", "runtime_revision"),),
        "no_effect_verified": (("scope_sha256", "scope_sha256"),),
        "artifact_hash_matches": (("expected_sha256", "artifact_sha256"),),
    }
    for fact_field, revision_field in bindings.get(verifier, ()):
        if facts.get(fact_field) != revision.get(revision_field):
            return False, f"fact-revision-mismatch:{fact_field}"
    return True, "facts-revision-bound"


def evaluate_criterion(
    criterion: Mapping[str, Any],
    evidence: Any,
    *,
    task_sha256: str,
    plan_sha256: str | None,
    now: str | None = None,
) -> dict[str, Any]:
    criterion_id = criterion.get("id")
    if not isinstance(criterion_id, str) or not criterion_id:
        return _unknown("<missing-id>", None, "criterion-id-invalid", None)
    contract = criterion_contract(criterion)
    if contract is None:
        return _unknown(criterion_id, None, "criterion-is-not-typed", None)
    verifier = str(contract["verifier"])
    domain = str(contract["domain"])
    if not isinstance(evidence, Mapping):
        return _unknown(criterion_id, verifier, "evidence-missing-or-unreadable", domain)
    if evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("kind") != EVIDENCE_KIND:
        return _unknown(criterion_id, verifier, "evidence-contract-invalid", domain)
    if evidence.get("criterion_id") != criterion_id or evidence.get("evidence_type") != verifier:
        return _unknown(criterion_id, verifier, "evidence-binding-mismatch", domain)
    source = evidence.get("source")
    authority = source.get("authority") if isinstance(source, Mapping) else None
    if authority not in contract["authorities"]:
        return _unknown(criterion_id, verifier, "evidence-authority-invalid", domain)
    source_reference = source.get("reference") if isinstance(source, Mapping) else None
    if not isinstance(source_reference, str) or not source_reference.strip():
        return _unknown(criterion_id, verifier, "evidence-source-reference-missing", domain)
    observed_at = _parse_time(evidence.get("observed_at"))
    current = _now(now)
    if observed_at is None:
        return _unknown(criterion_id, verifier, "evidence-time-invalid", domain)
    if observed_at > current:
        return _unknown(criterion_id, verifier, "evidence-from-future", domain)
    age = (current - observed_at).total_seconds()
    if age > int(contract["max_age_seconds"]):
        return _unknown(criterion_id, verifier, "evidence-stale", domain)
    validity = evidence.get("validity")
    if isinstance(validity, Mapping) and validity.get("not_after") is not None:
        not_after = _parse_time(validity.get("not_after"))
        if not_after is None or current > not_after:
            return _unknown(criterion_id, verifier, "evidence-validity-expired", domain)
    revision_ok, revision_reason = _revision_valid(
        contract,
        evidence,
        task_sha256=task_sha256,
        plan_sha256=plan_sha256,
    )
    if not revision_ok:
        return _unknown(criterion_id, verifier, revision_reason, domain)
    facts = evidence.get("facts")
    if not isinstance(facts, Mapping):
        return _unknown(criterion_id, verifier, "evidence-facts-missing", domain)
    revision = evidence.get("revision")
    assert isinstance(revision, Mapping)  # established by _revision_valid
    facts_ok, facts_reason = _facts_match_revision(verifier, revision, facts)
    if not facts_ok:
        return _unknown(criterion_id, verifier, facts_reason, domain)
    derived_state, reason = _fact_state(verifier, facts)
    claimed_state = evidence.get("status")
    if claimed_state is not None:
        if claimed_state not in STATES:
            return _unknown(criterion_id, verifier, "evidence-status-invalid", domain)
        if claimed_state != derived_state:
            return _unknown(criterion_id, verifier, "contradictory-evidence", domain)
    return {
        "criterion_id": criterion_id,
        "verifier": verifier,
        "domain": domain,
        "state": derived_state,
        "reason": reason,
        "authority": authority,
        "source_reference": source_reference,
        "observed_at": evidence.get("observed_at"),
        "age_seconds": age,
        "revision_reason": revision_reason,
    }


def evaluate_acceptance(
    criteria: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    *,
    task_id: str,
    run_id: str,
    task_sha256: str,
    plan_sha256: str | None,
    now: str | None = None,
) -> dict[str, Any]:
    evaluated_at = (_now(now)).isoformat().replace("+00:00", "Z")
    results = [
        evaluate_criterion(
            criterion,
            evidence.get(str(criterion.get("id"))),
            task_sha256=task_sha256,
            plan_sha256=plan_sha256,
            now=evaluated_at,
        )
        for criterion in criteria
    ]
    states = {item["state"] for item in results}
    if not results or UNKNOWN in states:
        state = UNKNOWN
    elif FAILED in states:
        state = FAILED
    elif states == {PASSED}:
        state = PASSED
    else:
        state = UNKNOWN
    domains = sorted({str(item["domain"]) for item in results if item.get("domain")})
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": EVALUATION_KIND,
        "task_id": task_id,
        "run_id": run_id,
        "task_sha256": task_sha256,
        "plan_sha256": plan_sha256,
        "evaluated_at": evaluated_at,
        "state": state,
        "automatic_terminalization": state == PASSED,
        "criteria": results,
        "domains": domains,
        "does_not_establish": [
            "merge_authority",
            "deployment_authority",
            "freshness_beyond_each_verifier_contract",
        ],
    }


def typed_evaluation_signal(value: Any) -> str | None:
    if not isinstance(value, Mapping) or value.get("kind") != EVALUATION_KIND:
        return None
    if value.get("schema_version") != SCHEMA_VERSION:
        return UNKNOWN
    state = value.get("state")
    if state not in STATES:
        return UNKNOWN
    automatic = value.get("automatic_terminalization")
    if state == PASSED and automatic is not True:
        return UNKNOWN
    if state != PASSED and automatic is True:
        return UNKNOWN
    return state
