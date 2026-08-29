from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bureau import doctor as doctor_module
from bureau import legacy
from bureau.doctor import (
    closeout_plan_projection,
    doctor_projection,
    review_closeout_plan,
    truth_drift_projection,
)
from bureau.state_backup import create_backup
from bureau.v2 import Registry, StateStore

NOW = "2026-07-07T12:00:00Z"
TASK_1 = "BUR-TEST-001-T001"


def make_state(root: Path) -> Path:
    state_root = root / "state"
    state_root.mkdir(exist_ok=True)
    StateStore(state_root / "bureau.sqlite3", state_root)
    return state_root


def add_running_work(state_root: Path) -> None:
    with sqlite3.connect(state_root / "bureau.sqlite3") as connection:
        connection.execute(
            "INSERT OR IGNORE INTO workers VALUES(?,?,?,?)",
            ("worker-doctor", "interactive-agent", "[]", NOW),
        )
        connection.execute(
            """
            INSERT INTO runs(
                run_id,task_id,worker_id,attempt,state,task_sha256,plan_sha256,
                envelope_json,envelope_sha256,workspace_path,workspace_branch,
                created_at,updated_at,heartbeat_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "BUR-RUN-DOCTOR",
                TASK_1,
                "worker-doctor",
                1,
                "running",
                "task-sha",
                "plan-sha",
                "{}",
                "envelope-sha",
                str(state_root / "workspace"),
                "task/doctor",
                NOW,
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?)",
            ("BUR-RUN-DOCTOR", "repo.alpha", "write", 1, NOW),
        )
        connection.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "BUR-RUN-DOCTOR",
                str(state_root.parent),
                str(state_root / "workspace"),
                "task/doctor",
                "a" * 40,
                "active",
                NOW,
                NOW,
                None,
            ),
        )


def write_restore_receipt(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "kind": "bureau_state_restore_test_receipt",
        "status": "verified",
        "tested_at": NOW,
        "manifest_sha256": "a" * 64,
        "authoritative_root_sha256": "b" * 64,
    }
    payload["receipt_sha256"] = legacy.sha256_json(payload)
    (root / "latest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_doctor_is_read_only_and_repair_plan_has_no_effect(
    registry_factory, tmp_path: Path
) -> None:
    root = registry_factory()
    state_root = make_state(root)
    database = state_root / "bureau.sqlite3"
    before = database.read_bytes()

    result = doctor_projection(
        root,
        state_root=state_root,
        backup_root=tmp_path / "no-backups",
        restore_receipt_root=tmp_path / "no-restores",
        now=NOW,
    )

    assert result["read_only"] is True
    assert result["effect"] == "none"
    assert result["healthy"] is False
    assert database.read_bytes() == before
    assert result["repair_plan"]["apply_contract_required"] is True
    assert result["repair_plan"]["proposal_count"] >= 2
    for proposal in result["repair_plan"]["proposals"]:
        assert proposal["effect"] == "none"
        assert len(proposal["dry_run_sha256"]) == 64
        assert proposal["required_authority"]
        assert proposal["apply_contract"]
        assert proposal["readback"]


def test_doctor_projects_flow_backup_restore_and_authority(
    registry_factory, tmp_path: Path
) -> None:
    root = registry_factory(2, mode="write")
    state_root = make_state(root)
    backup_root = tmp_path / "backups"
    create_backup(state_root=state_root, backup_root=backup_root)
    add_running_work(state_root)
    restore_root = tmp_path / "restore-receipts"
    write_restore_receipt(restore_root)

    result = doctor_projection(
        root,
        state_root=state_root,
        backup_root=backup_root,
        restore_receipt_root=restore_root,
        now=NOW,
    )
    control = result["control_plane"]
    metrics = control["metrics"]

    assert metrics["ready"]["value"] == 2
    assert metrics["claimable"]["value"] == 1
    assert metrics["in_flight"]["value"] == 1
    assert metrics["claims"]["value"] == 1
    assert metrics["workspaces"]["value"] == 1
    assert metrics["backup_age_seconds"]["value"] is not None
    assert metrics["restore_status"]["value"] == "verified"
    assert control["organs"]["backup"]["status"] == "verified"
    assert control["organs"]["restore"]["status"] == "verified"
    assert control["authority"]["operational"] == "Bureau StateStore"
    for organ in control["organs"].values():
        assert {"source", "freshness", "bounds", "authority", "status"} <= set(organ)



def test_doctor_counts_state_store_only_tasks_as_operational_truth(
    registry_factory, tmp_path: Path
) -> None:
    root = registry_factory()
    state_root = make_state(root)
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    store.import_registry_task_specs(registry)
    current = store.task_spec(TASK_1)
    assert current is not None
    new_task = json.loads(json.dumps(current["spec"]))
    new_task["id"] = "BUR-TEST-001-T999"
    new_task["title"] = "Doctor StateStore-only task"
    new_task["priority"] = {"lane": "next", "rank": 999}
    store.put_task_spec(
        new_task,
        idempotency_key="doctor-state-store-only-task",
        expected_revision=None,
        source="test-doctor",
    )

    result = doctor_projection(
        root,
        state_root=state_root,
        backup_root=tmp_path / "no-backups",
        restore_receipt_root=tmp_path / "no-restores",
        now=NOW,
    )

    assert result["control_plane"]["bounds"]["task_count"] == 4
    assert result["control_plane"]["metrics"]["ready"]["value"] == 4
    assert result["control_plane"]["authority"]["operational"] == "Bureau StateStore"


def test_dashboard_is_bounded_doctor_consumer(registry_factory, tmp_path: Path) -> None:
    root = registry_factory()
    state_root = make_state(root)
    result = doctor_projection(
        root,
        state_root=state_root,
        backup_root=tmp_path / "no-backups",
        restore_receipt_root=tmp_path / "no-restores",
        now=NOW,
    )
    dashboard = result["dashboard"]

    assert dashboard["kind"] == "bureau_control_plane_dashboard"
    assert dashboard["source"] == "bureau-control-plane-doctor"
    assert dashboard["read_only"] is True
    assert "tasks" not in dashboard
    assert "repository_balls" not in dashboard
    assert "next_actions" not in dashboard
    assert set(dashboard) == {
        "schema_version",
        "kind",
        "generated_at",
        "healthy",
        "metrics",
        "organs",
        "attention",
        "source",
        "read_only",
        "does_not_establish",
    }
    assert all("dry_run_sha256" in item for item in dashboard["attention"])


def test_truth_drift_marks_doctor_and_dashboard_unhealthy_with_bounded_attention(
    registry_factory, tmp_path: Path
) -> None:
    root = registry_factory()
    state_root = make_state(root)
    backup_root = tmp_path / "backups"
    create_backup(state_root=state_root, backup_root=backup_root)
    restore_root = tmp_path / "restore-receipts"
    write_restore_receipt(restore_root)
    github = {
        "schema_version": 1,
        "source": "github",
        "repository": "heimgewebe/bureau",
        "observed_at": NOW,
        "healthy": True,
        "binding_healthy": True,
        "blocked_reason": None,
        "hard_findings": [],
        "notes": [],
        "pull_requests": [],
        "historical_pull_requests": [
            {
                "repository": "heimgewebe/bureau",
                "number": 99,
                "url": "https://github.com/heimgewebe/bureau/pull/99",
                "title": "Merged implementation",
                "state": "MERGED",
                "is_draft": False,
                "head_ref": "feat/test",
                "head_sha": "a" * 40,
                "base_ref": "main",
                "merge_state": "UNKNOWN",
                "review_decision": "",
                "review_blocked": False,
                "checks": {"summary": "ci_unknown", "items": []},
                "updated_at": NOW,
                "observed_at": NOW,
                "binding": "bureau_task_marker",
                "confidence": 0.95,
                "task_id": TASK_1,
                "run_id": None,
                "ambiguous_reason": None,
                "notes": [],
            }
        ],
        "does_not_establish": ["task_completion", "merge_readiness"],
    }

    result = doctor_projection(
        root,
        state_root=state_root,
        github=github,
        backup_root=backup_root,
        restore_receipt_root=restore_root,
        now=NOW,
    )

    assert result["truth_drift"]["finding_count"] == 1
    assert result["control_plane"]["metrics"]["truth_drift"]["value"] == 1
    assert result["control_plane"]["organs"]["truth_drift"]["status"] == "attention"
    assert result["control_plane"]["healthy"] is False
    assert result["healthy"] is False
    assert result["dashboard"]["healthy"] is False
    attention = [
        item
        for item in result["dashboard"]["attention"]
        if item["code"] == "truth-drift-present"
    ]
    assert len(attention) == 1
    assert attention[0]["dry_run_sha256"] == result["closeout_plans"]["plan_set_sha256"]



def _truth_task(
    task_id: str,
    *,
    state: str = "ready",
    queue_lane: str | None = "later",
    compatibility_queue_lane: str | None = "later",
    github_state: str | None = None,
    github_updated_at: str | None = None,
    github_history: list[dict[str, object]] | None = None,
    receipts: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    github = (
        {
            "state": github_state,
            "number": 17,
            "head_sha": "a" * 40,
            "review_decision": None,
            "updated_at": github_updated_at or "",
        }
        if github_state is not None
        else None
    )
    return {
        "task_id": task_id,
        "effective_state": state,
        "queue_lane": queue_lane,
        "compatibility_queue_lane": compatibility_queue_lane,
        "github": github,
        "github_history": github_history or [],
        "receipts": receipts or [],
    }


def test_truth_drift_classifies_all_required_classes_and_renders_inert_plans() -> None:
    projection = {
        "tasks": [
            _truth_task(
                "TASK-MERGED",
                github_history=[
                    {
                        "state": "MERGED",
                        "number": 17,
                        "head_sha": "a" * 40,
                        "updated_at": "2026-08-29T07:00:00Z",
                    }
                ],
            ),
            _truth_task("TASK-VERIFIED", state="verified", compatibility_queue_lane="next"),
            _truth_task(
                "TASK-CLOSED",
                github_history=[
                    {
                        "state": "CLOSED",
                        "number": 18,
                        "review_decision": None,
                        "updated_at": "2026-08-29T07:01:00Z",
                    }
                ],
            ),
            _truth_task(
                "TASK-RUNTIME", receipts=[{"receipt_sha256": "b" * 64}]
            ),
            _truth_task(
                "TASK-QUEUE", queue_lane="next", compatibility_queue_lane="later"
            ),
        ]
    }

    drift = truth_drift_projection(projection)
    codes = {item["code"] for item in drift["findings"]}

    assert codes == {
        "merged-implementation-open-task",
        "verified-task-still-queued",
        "closed-pr-without-decision",
        "runtime-proof-without-closeout",
        "queue-state-lane-mismatch",
    }
    assert drift["read_only"] is True
    assert drift["effect"] == "none"
    assert all(item["acceptance_recheck_required"] is True for item in drift["findings"])
    assert all(len(item["finding_sha256"]) == 64 for item in drift["findings"])

    plans = closeout_plan_projection(drift)
    assert plans["plan_count"] == drift["finding_count"]
    assert plans["review_required"] is True
    assert plans["effect"] == "none"
    for plan in plans["plans"]:
        unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
        assert plan["plan_sha256"] == legacy.sha256_json(unsigned)
        assert plan["review_contract"]["status"] == "pending"
        assert plan["rollback_or_refusal"]["revision_drift"] == "refuse"
        assert "current-acceptance-contract" in plan["required_rechecks"]


def test_open_pr_suppresses_closed_attempt_even_after_later_metadata_update() -> None:
    drift = truth_drift_projection(
        {
            "tasks": [
                _truth_task(
                    "TASK-REPLACED",
                    github_state="OPEN",
                    github_updated_at="2026-08-29T07:00:00Z",
                    github_history=[
                        {
                            "state": "CLOSED",
                            "number": 12,
                            "review_decision": None,
                            "updated_at": "2026-08-29T08:00:00Z",
                        }
                    ],
                )
            ]
        }
    )

    assert drift["counts"]["closed-pr-without-decision"] == 0
    assert all(
        item["code"] != "closed-pr-without-decision" for item in drift["findings"]
    )



def test_ambiguous_open_bindings_suppress_closed_attempt_drift() -> None:
    task = _truth_task(
        "TASK-AMBIGUOUS-OPEN",
        github_history=[
            {
                "state": "CLOSED",
                "number": 12,
                "review_decision": None,
                "updated_at": "2026-08-29T08:00:00Z",
            }
        ],
    )
    task["github"] = {
        "binding": "ambiguous",
        "ambiguous_reason": "multiple-open-prs-for-task",
        "candidates": [20, 21],
    }

    drift = truth_drift_projection({"tasks": [task]})

    assert drift["counts"]["closed-pr-without-decision"] == 0
    assert all(
        item["code"] != "closed-pr-without-decision" for item in drift["findings"]
    )



def test_truth_drift_prefers_newest_lifecycle_evidence_over_highest_pr_number() -> None:
    drift = truth_drift_projection(
        {
            "tasks": [
                _truth_task(
                    "TASK-MERGED-ORDER",
                    github_history=[
                        {
                            "state": "MERGED",
                            "number": 99,
                            "head_sha": "c" * 40,
                            "updated_at": "2026-08-29T06:00:00Z",
                        },
                        {
                            "state": "MERGED",
                            "number": 42,
                            "head_sha": "d" * 40,
                            "updated_at": "2026-08-29T07:00:00Z",
                        },
                    ],
                )
            ]
        }
    )

    finding = next(
        item
        for item in drift["findings"]
        if item["code"] == "merged-implementation-open-task"
    )
    assert finding["evidence"]["pull_request"] == 42
    assert finding["evidence"]["head_sha"] == "d" * 40



def test_nonterminal_task_absent_from_compatibility_queue_is_not_lane_drift() -> None:
    drift = truth_drift_projection(
        {
            "tasks": [
                _truth_task(
                    "TASK-NOT-PROJECTED",
                    state="ready",
                    queue_lane="later",
                    compatibility_queue_lane=None,
                )
            ]
        }
    )

    assert drift["finding_count"] == 0
    assert drift["counts"]["queue-state-lane-mismatch"] == 0



def test_terminal_task_without_compatibility_queue_is_not_lane_drift() -> None:
    drift = truth_drift_projection(
        {
            "tasks": [
                _truth_task(
                    "TASK-DONE",
                    state="verified",
                    queue_lane="later",
                    compatibility_queue_lane=None,
                )
            ]
        }
    )

    assert drift["finding_count"] == 0
    assert drift["counts"]["queue-state-lane-mismatch"] == 0
    assert drift["counts"]["verified-task-still-queued"] == 0


def test_reviewed_closeout_plan_is_hash_bound_and_still_has_no_effect() -> None:
    drift = truth_drift_projection(
        {"tasks": [_truth_task("TASK-MERGED", github_state="MERGED")]}
    )
    plan = closeout_plan_projection(drift)["plans"][0]

    reviewed = review_closeout_plan(
        plan,
        expected_plan_sha256=plan["plan_sha256"],
        reviewer="independent-reviewer",
    )

    assert reviewed["effect"] == "none"
    assert reviewed["review"]["decision"] == "approved-for-fresh-recheck-only"
    assert reviewed["review"]["plan_sha256"] == plan["plan_sha256"]
    assert "task_completion" in reviewed["does_not_establish"]
    assert "queue_mutation_authority" in reviewed["does_not_establish"]
    assert len(reviewed["review_sha256"]) == 64

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        review_closeout_plan(
            plan, expected_plan_sha256="0" * 64, reviewer="independent-reviewer"
        )


def test_cli_observes_github_by_default(monkeypatch, tmp_path: Path, capsys) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    state_root = tmp_path / "state"
    observation = {"source": "github", "healthy": True, "pull_requests": []}
    seen: dict[str, object] = {}

    def observe(observed_root: Path, *, state_root: Path | None = None):
        seen["observe_root"] = observed_root
        seen["observe_state_root"] = state_root
        return observation

    def project(projected_root: Path, **kwargs):
        seen["project_root"] = projected_root
        seen["project_github"] = kwargs["github"]
        return {"kind": "doctor-test", "github_source": kwargs["github"]["source"]}

    monkeypatch.setattr(doctor_module, "observe_pull_requests", observe)
    monkeypatch.setattr(doctor_module, "doctor_projection", project)

    assert doctor_module.main(["--root", str(root), "--state-root", str(state_root)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert seen["observe_root"] == root.resolve()
    assert seen["observe_state_root"] == state_root
    assert seen["project_root"] == root.resolve()
    assert seen["project_github"] is observation
    assert payload["github_source"] == "github"


def test_cli_explicit_github_observations_skip_live_observer(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    observation_path = tmp_path / "github.json"
    observation = {"source": "fixture", "healthy": True, "pull_requests": []}
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    seen: dict[str, object] = {}

    def unexpected_observer(*args, **kwargs):
        raise AssertionError("explicit GitHub observations must not trigger a live fetch")

    def project(projected_root: Path, **kwargs):
        seen["project_root"] = projected_root
        seen["project_github"] = kwargs["github"]
        return {"kind": "doctor-test", "github_source": kwargs["github"]["source"]}

    monkeypatch.setattr(doctor_module, "observe_pull_requests", unexpected_observer)
    monkeypatch.setattr(doctor_module, "doctor_projection", project)

    assert (
        doctor_module.main(
            [
                "--root",
                str(root),
                "--github-observations",
                str(observation_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert seen["project_root"] == root.resolve()
    assert seen["project_github"] == observation
    assert payload["github_source"] == "fixture"
