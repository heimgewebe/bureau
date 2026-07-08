from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bureau.rlens_policy import (
    evaluate_registry_rlens_policy,
    evaluate_task_rlens_policy,
    evaluate_task_rlens_policy_report,
)
from bureau.schema_validation import SchemaSet

ROOT = Path(__file__).resolve().parents[1]
HEX64 = "a" * 64
HEX40 = "b" * 40


def context_ref() -> dict:
    return {
        "schema_version": 1,
        "repo": "bureau",
        "stem": "bureau-context",
        "manifest_sha256": HEX64,
        "bundle_commit": HEX40,
        "live_commit_at_claim": HEX40,
        "freshness_status": "fresh_exact",
        "task_profile": "repo_work",
        "preflight_status": "pass",
        "source": "grabowski.rlens_context_pack",
        "does_not_establish": ["repo_understood"],
    }


def task(**overrides: object) -> dict:
    value = {
        "schema_version": 1,
        "id": "BUR-TEST-T001",
        "initiative": "BUR-TEST",
        "title": "Repo work",
        "state": "planned",
        "goal": "Change repository code",
        "execution": {"mode": "interactive-agent", "policy": "review-before-effect"},
        "claims": [{"resource": "repo.bureau", "mode": "write", "isolation": "worktree"}],
        "acceptance": [{"id": "done", "assertion": "done"}],
        "metadata": {},
    }
    value.update(overrides)
    return value


def test_modes_are_deterministic_for_explicit_task_policy() -> None:
    observed = []
    for mode in ["opportunistic", "required", "strict", "live-first", "external-safe"]:
        result = evaluate_task_rlens_policy_report(
            task(rlens_policy={"mode": mode, "task_profile": "repo_work"})
        )
        observed.append((result["mode"], result["requirement"]))
    assert observed == [
        ("opportunistic", "optional"),
        ("required", "context_ref_or_explicit_skip_reason"),
        ("strict", "fresh_context_ref_or_explicit_skip_or_block_reason"),
        ("live-first", "live_tools_primary_rlens_optional"),
        ("external-safe", "context_pack_only_or_explicit_skip_reason"),
    ]


def test_required_task_without_context_or_skip_blocks() -> None:
    result = evaluate_task_rlens_policy(
        task(rlens_policy={"mode": "required", "task_profile": "repo_work"})
    )
    assert result["status"] == "blocked"
    assert result["block_reason"] == "rlens_policy_required_requires_context_ref_or_skip_reason"


def test_required_task_with_skip_reason_is_machine_readable_non_blocking() -> None:
    result = evaluate_task_rlens_policy_report(
        task(
            rlens_policy={
                "mode": "required",
                "task_profile": "repo_work",
                "skip_reason": "not_yet_generated",
            }
        )
    )
    assert result["status"] == "skip-recorded"
    assert result["skip_reason_present"] is True
    assert result["context_ref_present"] is False


def test_strict_task_with_context_ref_passes() -> None:
    result = evaluate_task_rlens_policy_report(
        task(
            rlens_policy={"mode": "strict", "task_profile": "pr_review"},
            rlens_context_ref=context_ref(),
        )
    )
    assert result["status"] == "ok"
    assert result["context_ref_present"] is True


def test_live_first_tasks_remain_unblocked_without_rlens() -> None:
    result = evaluate_task_rlens_policy_report(
        task(
            title="Deploy runtime service",
            goal="Restart service after live checks",
            rlens_policy={"mode": "live-first", "task_profile": "runtime"},
        )
    )
    assert result["status"] == "live-first"
    assert result["context_ref_present"] is False
    assert result["reasons"] == ["live-first task: rLens is optional repo/doc context"]


def test_external_safe_requires_context_pack_or_skip_reason() -> None:
    blocked = evaluate_task_rlens_policy_report(
        task(rlens_policy={"mode": "external-safe", "task_profile": "external"})
    )
    skipped = evaluate_task_rlens_policy_report(
        task(
            rlens_policy={
                "mode": "external-safe",
                "task_profile": "external",
                "skip_reason": "external_safe_export_blocked",
            }
        )
    )
    assert blocked["status"] == "block"
    assert skipped["status"] == "skip-recorded"


def test_registry_policy_report_contains_non_claims_and_blockers() -> None:
    class Wrapper:
        def __init__(self, raw: dict) -> None:
            self.raw = raw

    report = evaluate_registry_rlens_policy(
        {
            "A": Wrapper(task(id="A", rlens_policy={"mode": "required"})),
            "B": Wrapper(task(id="B", rlens_policy={"mode": "live-first"})),
        }
    )
    assert report["summary"] == {"tasks": 2, "blockers": 1, "policy_missing": 0}
    assert report["blockers"][0]["task_id"] == "A"
    assert "merge_readiness" in report["does_not_establish"]


def test_inferred_required_task_is_reported_but_not_strict_blocker() -> None:
    result = evaluate_task_rlens_policy_report(task(title="Repository code change"))
    assert result["mode"] == "required"
    assert result["status"] == "policy-missing"
    assert result["policy_source"] == "inferred"
    assert result["skip_reason_present"] is False


def test_task_schema_accepts_rlens_policy() -> None:
    SchemaSet(ROOT / "schemas").validate(
        "task",
        task(rlens_policy={"mode": "strict", "task_profile": "pr_review"}),
        "task",
    )


def test_cli_reports_single_task_policy() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bureau.cli",
            "--root",
            str(ROOT),
            "--json",
            "rlens-policy",
            "--task-id",
            "BUR-2026-002-T003",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(ROOT / "src")},
    )
    report = json.loads(result.stdout)
    assert report["kind"] == "bureau.rlens_task_policy_report"
    assert report["summary"]["tasks"] == 1
    assert report["tasks"][0]["task_id"] == "BUR-2026-002-T003"
