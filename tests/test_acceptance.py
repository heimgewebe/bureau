from __future__ import annotations

import pytest

from bureau.acceptance import (
    EVALUATION_KIND,
    EVIDENCE_KIND,
    FAILED,
    PASSED,
    UNKNOWN,
    VERIFIER_CONTRACTS,
    evaluate_acceptance,
    evaluate_criterion,
    typed_criterion_contracts,
)
from bureau.review_steward import evidence_signal

TASK_SHA = "a" * 64
PLAN_SHA = "b" * 64
HEAD_SHA = "c" * 40
MERGE_SHA = "d" * 40
RUNTIME_SHA = "e" * 40
ARTIFACT_SHA = "f" * 64
SCOPE_SHA = "1" * 64
NOW = "2026-08-10T20:00:00Z"
OBSERVED = "2026-08-10T19:59:00Z"


def criterion(criterion_id: str, verifier: str) -> dict[str, object]:
    return {
        "id": criterion_id,
        "assertion": f"prove {criterion_id}",
        "evidence_type": "object",
        "verifier": verifier,
    }


def revision_for(verifier: str) -> dict[str, str]:
    value = {"task_sha256": TASK_SHA, "plan_sha256": PLAN_SHA}
    if verifier == "code_merged":
        value.update(head_sha=HEAD_SHA, merge_commit_sha=MERGE_SHA)
    elif verifier == "required_ci_green":
        value.update(head_sha=HEAD_SHA)
    elif verifier == "deployment_complete":
        value.update(deployment_revision="deployment-42")
    elif verifier == "runtime_commit_contains":
        value.update(required_commit=HEAD_SHA, observed_commit=RUNTIME_SHA)
    elif verifier == "live_probe_passed":
        value.update(runtime_revision=RUNTIME_SHA)
    elif verifier == "no_effect_verified":
        value.update(scope_sha256=SCOPE_SHA)
    elif verifier == "artifact_hash_matches":
        value.update(artifact_sha256=ARTIFACT_SHA)
    return value


def primary_evidence(
    criterion_id: str,
    verifier: str,
    *,
    authority: str,
    facts: dict[str, object],
    observed_at: str = OBSERVED,
    status: str | None = None,
) -> dict[str, object]:
    revision = revision_for(verifier)
    bound_facts = dict(facts)
    fact_bindings = {
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
    for fact_field, revision_field in fact_bindings.get(verifier, ()):
        bound_facts.setdefault(fact_field, revision[revision_field])
    value: dict[str, object] = {
        "schema_version": 1,
        "kind": EVIDENCE_KIND,
        "criterion_id": criterion_id,
        "evidence_type": verifier,
        "source": {"authority": authority, "reference": f"test:{criterion_id}"},
        "observed_at": observed_at,
        "revision": revision,
        "facts": bound_facts,
    }
    if status is not None:
        value["status"] = status
    return value


PASS_CASES = [
    (
        "code_merged",
        "github",
        {"merged": True, "head_sha": HEAD_SHA, "merge_commit_sha": MERGE_SHA},
    ),
    (
        "required_ci_green",
        "github",
        {"checks": [{"name": "validate", "state": "success"}]},
    ),
    (
        "deployment_complete",
        "grabowski",
        {"completed": True, "receipt_sha256": "2" * 64},
    ),
    (
        "runtime_commit_contains",
        "target-runtime",
        {"contains_required_commit": True},
    ),
    ("live_probe_passed", "target-runtime", {"result": "passed"}),
    (
        "manual_observation",
        "manual",
        {"accepted": True, "observer": "operator", "observation": "live result accepted"},
    ),
    (
        "duration_soak_completed",
        "target-runtime",
        {"completed": True, "required_seconds": 60, "observed_seconds": 61},
    ),
    ("no_effect_verified", "bureau", {"effect_count": 0}),
    (
        "artifact_hash_matches",
        "artifact-store",
        {"expected_sha256": ARTIFACT_SHA, "observed_sha256": ARTIFACT_SHA},
    ),
]


@pytest.mark.parametrize(("verifier", "authority", "facts"), PASS_CASES)
def test_each_typed_primary_evidence_class_can_pass(
    verifier: str,
    authority: str,
    facts: dict[str, object],
) -> None:
    result = evaluate_criterion(
        criterion("proof", verifier),
        primary_evidence("proof", verifier, authority=authority, facts=facts),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == PASSED
    assert result["domain"] == VERIFIER_CONTRACTS[verifier]["domain"]
    assert result["revision_reason"] == "revision-bound"


def test_all_criteria_require_named_typed_contracts() -> None:
    criteria = [criterion(name, name) for name in VERIFIER_CONTRACTS]
    contracts = typed_criterion_contracts(criteria)

    assert contracts["complete"] is True
    assert contracts["invalid_criterion_ids"] == []
    assert {item["verifier"] for item in contracts["criteria"]} == set(VERIFIER_CONTRACTS)
    assert all(item["revision_binding"] for item in contracts["criteria"])
    assert all(item["max_age_seconds"] > 0 for item in contracts["criteria"])


def test_untyped_criterion_is_unknown_not_success() -> None:
    result = evaluate_criterion(
        {"id": "proof", "assertion": "legacy prose"},
        {"anything": True},
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "criterion-is-not-typed"


def test_stale_evidence_remains_unknown() -> None:
    result = evaluate_criterion(
        criterion("probe", "live_probe_passed"),
        primary_evidence(
            "probe",
            "live_probe_passed",
            authority="target-runtime",
            facts={"result": "passed"},
            observed_at="2026-08-10T19:00:00Z",
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "evidence-stale"


def test_revision_drift_remains_unknown() -> None:
    evidence = primary_evidence(
        "merge",
        "code_merged",
        authority="github",
        facts={"merged": True, "head_sha": HEAD_SHA, "merge_commit_sha": MERGE_SHA},
    )
    evidence["revision"] = {**revision_for("code_merged"), "task_sha256": "9" * 64}

    result = evaluate_criterion(
        criterion("merge", "code_merged"),
        evidence,
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "task-revision-mismatch"


def test_missing_source_reference_remains_unknown() -> None:
    evidence = primary_evidence(
        "merge",
        "code_merged",
        authority="github",
        facts={"merged": True},
    )
    evidence["source"] = {"authority": "github"}

    result = evaluate_criterion(
        criterion("merge", "code_merged"),
        evidence,
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "evidence-source-reference-missing"


def test_critical_fact_must_match_bound_revision() -> None:
    evidence = primary_evidence(
        "merge",
        "code_merged",
        authority="github",
        facts={"merged": True},
    )
    evidence["facts"] = {
        **evidence["facts"],
        "head_sha": "9" * 40,
    }

    result = evaluate_criterion(
        criterion("merge", "code_merged"),
        evidence,
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "fact-revision-mismatch:head_sha"


def test_contradictory_claimed_status_is_unknown() -> None:
    result = evaluate_criterion(
        criterion("merge", "code_merged"),
        primary_evidence(
            "merge",
            "code_merged",
            authority="github",
            facts={"merged": True, "head_sha": HEAD_SHA, "merge_commit_sha": MERGE_SHA},
            status=FAILED,
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "contradictory-evidence"


def test_explicit_ci_failure_is_failed_without_inventing_missing_facts() -> None:
    result = evaluate_criterion(
        criterion("ci", "required_ci_green"),
        primary_evidence(
            "ci",
            "required_ci_green",
            authority="github",
            facts={"checks": [{"name": "validate", "state": "failure"}]},
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == FAILED
    assert result["reason"] == "required-check-failed"


def test_product_merge_and_deployment_remain_separate_evidence_domains() -> None:
    criteria = [criterion("merge", "code_merged"), criterion("deploy", "deployment_complete")]
    evidence = {
        "merge": primary_evidence(
            "merge",
            "code_merged",
            authority="github",
            facts={"merged": True, "head_sha": HEAD_SHA, "merge_commit_sha": MERGE_SHA},
        )
    }

    result = evaluate_acceptance(
        criteria,
        evidence,
        task_id="TASK-1",
        run_id="RUN-1",
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["automatic_terminalization"] is False
    assert result["domains"] == ["deployment", "product"]
    assert {item["criterion_id"]: item["state"] for item in result["criteria"]} == {
        "merge": PASSED,
        "deploy": UNKNOWN,
    }


def test_only_all_passed_criteria_authorize_terminalization() -> None:
    criteria = [criterion("manual", "manual_observation")]
    evidence = {
        "manual": primary_evidence(
            "manual",
            "manual_observation",
            authority="manual",
            facts={"accepted": True, "observer": "operator", "observation": "confirmed"},
        )
    }

    result = evaluate_acceptance(
        criteria,
        evidence,
        task_id="TASK-1",
        run_id="RUN-1",
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["kind"] == EVALUATION_KIND
    assert result["state"] == PASSED
    assert result["automatic_terminalization"] is True


def test_review_steward_does_not_promote_typed_unknown_to_pass() -> None:
    typed = {
        "schema_version": 1,
        "kind": EVALUATION_KIND,
        "state": UNKNOWN,
        "automatic_terminalization": False,
    }

    assert evidence_signal(typed) == "present"
    assert (
        evidence_signal({**typed, "state": PASSED, "automatic_terminalization": True}) == "passed"
    )
    assert evidence_signal({**typed, "state": FAILED}) == "failed"
    assert evidence_signal({**typed, "state": PASSED}) == "present"
