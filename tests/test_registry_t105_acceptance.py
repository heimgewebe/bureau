"""T105's legacy claims require a source-authenticated, revision-bound observation."""

from __future__ import annotations

import json
from pathlib import Path

from bureau.acceptance import (
    EVIDENCE_KIND,
    FAILED,
    PASSED,
    UNKNOWN,
    evaluate_acceptance,
    validate_acceptance_contract,
)

TASK_PATH = (
    Path(__file__).resolve().parents[1]
    / "registry/tasks/GRABOWSKI-OPERATOR-SURFACE-V1-T105.json"
)
EXPECTED_IDS = (
    "repository-bound-guard",
    "typed-nonrepo-deny",
    "atomic-safety",
    "regression-tests",
    "live-proof",
)
TASK_SHA = "a" * 64
PLAN_SHA = "b" * 64
NOW = "2026-11-14T07:00:00Z"


def _task() -> dict:
    return json.loads(TASK_PATH.read_text(encoding="utf-8"))


def _evidence(task: dict) -> dict:
    result = {}
    for criterion in task["acceptance"]:
        scope = criterion["verifier_config"]["observation_scope"]
        result[criterion["id"]] = {
            "schema_version": 1,
            "kind": EVIDENCE_KIND,
            "criterion_id": criterion["id"],
            "evidence_type": "manual_observation",
            "source": {
                "authority": "manual",
                "reference": "git:heimgewebe/grabowski:exact-head-source-and-test-receipts",
            },
            "observed_at": "2026-11-14T06:59:00Z",
            "revision": {
                "task_sha256": TASK_SHA,
                "plan_sha256": PLAN_SHA,
                "observation_scope": scope,
            },
            "facts": {
                "accepted": True,
                "observer": "independent-observer",
                "observation": "Source- and run-bound test and live behavior observed",
                "observation_scope": scope,
            },
        }
    return result


def _evaluate(task: dict, evidence: dict, authenticated: set[str]) -> dict:
    return evaluate_acceptance(
        task["acceptance"],
        evidence,
        task_id=task["id"],
        run_id="BUR-RUN-T105-REGRESSION",
        task_sha256=TASK_SHA,
        plan_sha256=PLAN_SHA,
        now=NOW,
        authenticated_criterion_ids=authenticated,
    )


def test_t105_keeps_five_specific_typed_acceptance_criteria() -> None:
    task = _task()
    criteria = task["acceptance"]
    assert tuple(item["id"] for item in criteria) == EXPECTED_IDS
    assert all(item["assertion"] for item in criteria)
    assert all(item["evidence_type"] == "object" for item in criteria)
    assert all(item["verifier"] == "manual_observation" for item in criteria)
    assert all(
        item["verifier_config"] == {
            "observation_scope": f"grabowski:operator-surface:t105:{item['id']}"
        }
        for item in criteria
    )
    validate_acceptance_contract(task)


def test_t105_claimed_pass_is_not_accepted_without_independent_authentication() -> None:
    task = _task()
    evidence = _evidence(task)
    assert _evaluate(task, evidence, set())["state"] == UNKNOWN
    assert _evaluate(task, evidence, set(EXPECTED_IDS[:-1]))["state"] == UNKNOWN
    assert _evaluate(task, evidence, set(EXPECTED_IDS))["state"] == PASSED

    evidence["atomic-safety"]["facts"]["accepted"] = False
    assert _evaluate(task, evidence, set(EXPECTED_IDS))["state"] == FAILED
