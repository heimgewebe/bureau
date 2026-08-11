from __future__ import annotations

import json

import pytest

import bureau.review_steward as review_steward
from bureau.acceptance import (
    EVALUATION_KIND,
    EVIDENCE_KIND,
    FAILED,
    PASSED,
    UNKNOWN,
    VERIFIER_CONTRACTS,
    typed_criterion_contracts,
)
from bureau.acceptance import (
    evaluate_acceptance as _evaluate_acceptance,
)
from bureau.acceptance import (
    evaluate_criterion as _evaluate_criterion,
)
from bureau.core import Registry
from bureau.review_steward import evidence_signal

TASK_SHA = "a" * 64
PLAN_SHA = "b" * 64
HEAD_SHA = "c" * 40
BASE_REF = "main"
MERGE_SHA = "d" * 40
RUNTIME_SHA = "e" * 40
ARTIFACT_SHA = "f" * 64
SCOPE_SHA = "1" * 64
NOW = "2026-08-10T20:00:00Z"
OBSERVED = "2026-08-10T19:59:00Z"


def evaluate_criterion(*args, **kwargs):
    kwargs.setdefault("source_authenticated", True)
    return _evaluate_criterion(*args, **kwargs)


def evaluate_acceptance(criteria, evidence, **kwargs):
    kwargs.setdefault("authenticated_criterion_ids", set(evidence))
    return _evaluate_acceptance(criteria, evidence, **kwargs)


def criterion(
    criterion_id: str,
    verifier: str,
    verifier_config: dict[str, object] | None = None,
) -> dict[str, object]:
    configs: dict[str, dict[str, object]] = {
        "code_merged": {
            "repository": "heimgewebe/test",
            "pull_request": 7,
            "head_sha": HEAD_SHA,
            "base_ref": BASE_REF,
        },
        "required_ci_green": {
            "repository": "heimgewebe/test",
            "pull_request": 7,
            "head_sha": HEAD_SHA,
            "base_ref": BASE_REF,
            "required_checks": ["validate"],
        },
        "deployment_complete": {"deployment_revision": "deployment-42"},
        "runtime_commit_contains": {
            "repository": "heimgewebe/test",
            "required_commit": HEAD_SHA,
        },
        "live_probe_passed": {"runtime_revision": RUNTIME_SHA, "probe_id": "health"},
        "manual_observation": {"observation_scope": "manual:test"},
        "duration_soak_completed": {
            "required_seconds": 60,
            "observation_scope": "soak:test",
        },
        "no_effect_verified": {"scope_sha256": SCOPE_SHA},
        "artifact_hash_matches": {"artifact_sha256": ARTIFACT_SHA},
    }
    value: dict[str, object] = {
        "id": criterion_id,
        "assertion": f"prove {criterion_id}",
        "evidence_type": "object",
        "verifier": verifier,
    }
    config = dict(configs.get(verifier, {}))
    if verifier_config is not None:
        config.update(verifier_config)
    if config:
        value["verifier_config"] = config
    return value


def revision_for(verifier: str) -> dict[str, str]:
    value = {"task_sha256": TASK_SHA, "plan_sha256": PLAN_SHA}
    if verifier == "code_merged":
        value.update(head_sha=HEAD_SHA, base_ref=BASE_REF, merge_commit_sha=MERGE_SHA)
    elif verifier == "required_ci_green":
        value.update(head_sha=HEAD_SHA, base_ref=BASE_REF)
    elif verifier == "deployment_complete":
        value.update(deployment_revision="deployment-42")
    elif verifier == "runtime_commit_contains":
        value.update(required_commit=HEAD_SHA, observed_commit=RUNTIME_SHA)
    elif verifier == "live_probe_passed":
        value.update(runtime_revision=RUNTIME_SHA, probe_id="health")
    elif verifier == "manual_observation":
        value.update(observation_scope="manual:test")
    elif verifier == "duration_soak_completed":
        value.update(observation_scope="soak:test")
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
        "code_merged": (
            ("head_sha", "head_sha"),
            ("base_ref", "base_ref"),
            ("merge_commit_sha", "merge_commit_sha"),
        ),
        "required_ci_green": (("head_sha", "head_sha"), ("base_ref", "base_ref")),
        "deployment_complete": (("deployment_revision", "deployment_revision"),),
        "runtime_commit_contains": (
            ("required_commit", "required_commit"),
            ("observed_commit", "observed_commit"),
        ),
        "live_probe_passed": (
            ("runtime_revision", "runtime_revision"),
            ("probe_id", "probe_id"),
        ),
        "manual_observation": (("observation_scope", "observation_scope"),),
        "duration_soak_completed": (("observation_scope", "observation_scope"),),
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
        {
            "complete": True,
            "checks": [{"name": "validate", "state": "success"}],
        },
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
        {"completed": True, "observed_seconds": 61},
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


def test_registry_schema_accepts_frozen_verifier_configuration(
    registry_factory,
) -> None:
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["acceptance"] = [
        criterion(
            "ci",
            "required_ci_green",
            {"required_checks": ["validate (3.10)", "validate (3.12)"]},
        ),
        criterion(
            "soak",
            "duration_soak_completed",
            {"required_seconds": 3600},
        ),
    ]
    task_path.write_text(json.dumps(task), encoding="utf-8")

    registry = Registry.load(root)

    configured = registry.tasks["BUR-TEST-001-T001"].acceptance
    assert configured[0]["verifier_config"]["required_checks"] == [
        "validate (3.10)",
        "validate (3.12)",
    ]
    assert configured[1]["verifier_config"]["required_seconds"] == 3600


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
            facts={
                "complete": True,
                "checks": [{"name": "validate", "state": "failure"}],
            },
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == FAILED
    assert result["reason"] == "required-check-failed"


def _unmerged_evidence() -> dict[str, object]:
    evidence = primary_evidence(
        "merge",
        "code_merged",
        authority="github",
        facts={"merged": False, "merge_commit_sha": None},
    )
    evidence["revision"] = {**evidence["revision"], "merge_commit_sha": None}
    return evidence


def test_authenticated_unmerged_pr_is_known_failure() -> None:
    result = evaluate_criterion(
        criterion("merge", "code_merged"),
        _unmerged_evidence(),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == FAILED
    assert result["reason"] == "merge-observed-not-complete"


def test_negative_merge_with_commit_sha_is_incomplete_not_failure() -> None:
    evidence = _unmerged_evidence()
    evidence["revision"] = {**evidence["revision"], "merge_commit_sha": MERGE_SHA}
    evidence["facts"] = {**evidence["facts"], "merge_commit_sha": MERGE_SHA}

    result = evaluate_criterion(
        criterion("merge", "code_merged"),
        evidence,
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "merge-facts-incomplete"


def test_known_failure_dominates_unknown_conjunct() -> None:
    criteria = [
        criterion("merge", "code_merged"),
        criterion("deploy", "deployment_complete"),
    ]
    result = evaluate_acceptance(
        criteria,
        {"merge": _unmerged_evidence()},
        task_id="TASK-1",
        run_id="RUN-1",
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert [item["state"] for item in result["criteria"]] == [FAILED, UNKNOWN]
    assert result["state"] == FAILED
    assert result["automatic_terminalization"] is False


def test_required_ci_rejects_incomplete_observation() -> None:
    result = evaluate_criterion(
        criterion(
            "ci",
            "required_ci_green",
            {"required_checks": ["validate", "codeql"]},
        ),
        primary_evidence(
            "ci",
            "required_ci_green",
            authority="github",
            facts={
                "complete": False,
                "checks": [{"name": "validate", "state": "success"}],
            },
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "required-check-observation-incomplete"


def test_required_ci_rejects_missing_required_check_even_with_optional_green() -> None:
    result = evaluate_criterion(
        criterion(
            "ci",
            "required_ci_green",
            {"required_checks": ["validate", "codeql"]},
        ),
        primary_evidence(
            "ci",
            "required_ci_green",
            authority="github",
            facts={
                "complete": True,
                "checks": [
                    {"name": "validate", "state": "success"},
                    {"name": "optional", "state": "success"},
                ],
            },
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "required-check-missing"


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        pytest.param(10**10000, id="oversized-int"),
    ],
)
def test_soak_rejects_non_finite_or_boolean_observation(value) -> None:
    result = evaluate_criterion(
        criterion("soak", "duration_soak_completed"),
        primary_evidence(
            "soak",
            "duration_soak_completed",
            authority="target-runtime",
            facts={"completed": True, "observed_seconds": value},
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "soak-observation-invalid"


def test_required_ci_rejects_caller_supplied_required_set_drift() -> None:
    result = evaluate_criterion(
        criterion(
            "ci",
            "required_ci_green",
            {"required_checks": ["validate", "codeql"]},
        ),
        primary_evidence(
            "ci",
            "required_ci_green",
            authority="github",
            facts={
                "complete": True,
                "required_checks": ["validate"],
                "checks": [
                    {"name": "validate", "state": "success"},
                    {"name": "codeql", "state": "success"},
                ],
            },
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "required-check-set-mismatch"


def test_oversized_frozen_soak_requirement_is_invalid_without_exception() -> None:
    result = evaluate_criterion(
        criterion(
            "soak",
            "duration_soak_completed",
            {"required_seconds": 10**10000},
        ),
        primary_evidence(
            "soak",
            "duration_soak_completed",
            authority="target-runtime",
            facts={"completed": True, "observed_seconds": 60},
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "criterion-is-not-typed"


def test_soak_requirement_is_frozen_in_criterion() -> None:
    result = evaluate_criterion(
        criterion(
            "soak",
            "duration_soak_completed",
            {"required_seconds": 3600},
        ),
        primary_evidence(
            "soak",
            "duration_soak_completed",
            authority="target-runtime",
            facts={"completed": True, "observed_seconds": 0},
        ),
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == FAILED
    assert result["reason"] == "soak-duration-too-short"


def test_merge_and_required_ci_must_share_exact_head_revision() -> None:
    other_head = "9" * 40
    criteria = [
        criterion("merge", "code_merged"),
        criterion("ci", "required_ci_green", {"head_sha": other_head}),
    ]
    merge = primary_evidence(
        "merge",
        "code_merged",
        authority="github",
        facts={"merged": True, "head_sha": HEAD_SHA, "merge_commit_sha": MERGE_SHA},
    )
    ci = primary_evidence(
        "ci",
        "required_ci_green",
        authority="github",
        facts={
            "complete": True,
            "checks": [{"name": "validate", "state": "success"}],
        },
    )
    ci["revision"] = {**ci["revision"], "head_sha": other_head}
    ci["facts"] = {**ci["facts"], "head_sha": other_head}

    result = evaluate_acceptance(
        criteria,
        {"merge": merge, "ci": ci},
        task_id="TASK-1",
        run_id="RUN-1",
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert [item["state"] for item in result["criteria"]] == [PASSED, PASSED]
    assert result["state"] == UNKNOWN
    assert result["automatic_terminalization"] is False
    assert result["cross_criterion_revision"]["conflicts"] == [
        {
            "field": "head_sha",
            "criteria": {"merge": HEAD_SHA, "ci": other_head},
        }
    ]


def test_independent_pull_requests_may_have_distinct_heads() -> None:
    other_head = "8" * 40
    other_merge = "7" * 40
    criteria = [
        criterion("merge-a", "code_merged"),
        criterion(
            "merge-b",
            "code_merged",
            {"pull_request": 8, "head_sha": other_head},
        ),
    ]
    merge_a = primary_evidence(
        "merge-a",
        "code_merged",
        authority="github",
        facts={"merged": True, "head_sha": HEAD_SHA, "merge_commit_sha": MERGE_SHA},
    )
    merge_b = primary_evidence(
        "merge-b",
        "code_merged",
        authority="github",
        facts={"merged": True, "head_sha": other_head, "merge_commit_sha": other_merge},
    )
    merge_b["revision"] = {
        **merge_b["revision"],
        "head_sha": other_head,
        "merge_commit_sha": other_merge,
    }
    merge_b["facts"] = {
        **merge_b["facts"],
        "head_sha": other_head,
        "merge_commit_sha": other_merge,
    }

    result = evaluate_acceptance(
        criteria,
        {"merge-a": merge_a, "merge-b": merge_b},
        task_id="TASK-1",
        run_id="RUN-1",
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert [item["state"] for item in result["criteria"]] == [PASSED, PASSED]
    assert result["state"] == PASSED
    assert result["automatic_terminalization"] is True
    assert result["cross_criterion_revision"]["conflicts"] == []
    assert result["cross_criterion_revision"]["shared_fields"]["head_sha"] == {
        "merge-a": HEAD_SHA,
        "merge-b": other_head,
    }


def test_caller_claimed_authority_without_authentication_is_unknown() -> None:
    evidence = primary_evidence(
        "merge",
        "code_merged",
        authority="github",
        facts={"merged": True, "head_sha": HEAD_SHA, "merge_commit_sha": MERGE_SHA},
    )

    result = _evaluate_criterion(
        criterion("merge", "code_merged"),
        evidence,
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "evidence-source-unauthenticated"


def test_timestamp_overflow_is_invalid_evidence_not_exception() -> None:
    evidence = primary_evidence(
        "merge",
        "code_merged",
        authority="github",
        facts={"merged": True, "head_sha": HEAD_SHA, "merge_commit_sha": MERGE_SHA},
        observed_at="9999-12-31T23:59:59-23:59",
    )

    result = evaluate_criterion(
        criterion("merge", "code_merged"),
        evidence,
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == "evidence-time-invalid"


@pytest.mark.parametrize(
    ("verifier", "revision_field", "bad_value", "fact_field"),
    [
        ("code_merged", "head_sha", "9" * 40, "head_sha"),
        ("required_ci_green", "head_sha", "9" * 40, "head_sha"),
        ("deployment_complete", "deployment_revision", "deployment-evil", "deployment_revision"),
        ("runtime_commit_contains", "required_commit", "9" * 40, "required_commit"),
        ("live_probe_passed", "runtime_revision", "runtime-evil", "runtime_revision"),
        ("manual_observation", "observation_scope", "manual:evil", "observation_scope"),
        ("no_effect_verified", "scope_sha256", "9" * 64, "scope_sha256"),
        ("artifact_hash_matches", "artifact_sha256", "9" * 64, "expected_sha256"),
    ],
)
def test_verifier_targets_cannot_be_self_selected_by_evidence(
    verifier: str, revision_field: str, bad_value: str, fact_field: str
) -> None:
    pass_case = next(item for item in PASS_CASES if item[0] == verifier)
    evidence = primary_evidence(
        "proof", verifier, authority=pass_case[1], facts=dict(pass_case[2])
    )
    evidence["revision"] = {**evidence["revision"], revision_field: bad_value}
    evidence["facts"] = {**evidence["facts"], fact_field: bad_value}

    result = evaluate_criterion(
        criterion("proof", verifier),
        evidence,
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
    )

    assert result["state"] == UNKNOWN
    assert result["reason"] == f"criterion-target-mismatch:{revision_field}"


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


def test_review_steward_requests_supported_merged_at_field(monkeypatch) -> None:
    observed: list[str] = []

    monkeypatch.setattr(review_steward.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run_command(cwd, argv, timeout=20):
        observed.extend(argv)
        return {
            "ok": True,
            "returncode": 0,
            "stdout": (
                '{"number":1876,"state":"MERGED","isDraft":false,'
                '"mergedAt":"2026-08-10T22:00:00Z","reviewDecision":"APPROVED",'
                '"mergeStateStatus":"CLEAN","statusCheckRollup":[]}'
            ),
            "stderr": "",
        }

    monkeypatch.setattr(review_steward, "run_command", fake_run_command)
    raw_status = review_steward.gh_pr_status({"pr": 1876, "repo": "/tmp"})
    fields = observed[observed.index("--json") + 1].split(",")

    assert "mergedAt" in fields
    assert "merged" not in fields
    assert review_steward.normalize_pr_status(raw_status)["merged"] is True
