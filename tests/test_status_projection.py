from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bureau.cli import main
from bureau.status_projection import (
    AI_AUTHORITY_BOUNDARY,
    PROJECTION_DOES_NOT_ESTABLISH,
    STATUS_PROJECTION_SCHEMA_VERSION,
    status_projection,
)
from bureau.v2 import StateStore

NOW = "2026-07-07T12:00:00Z"

TASK_1 = "BUR-TEST-001-T001"
TASK_2 = "BUR-TEST-001-T002"


def make_state(root: Path) -> Path:
    state_root = root / "state"
    state_root.mkdir(exist_ok=True)
    StateStore(state_root / "bureau.sqlite3", state_root)
    return state_root


def connect(state_root: Path) -> sqlite3.Connection:
    return sqlite3.connect(state_root / "bureau.sqlite3")


def add_run(
    state_root: Path,
    run_id: str,
    task_id: str,
    *,
    state: str = "running",
    branch: str | None = None,
    workspace_path: str | None = None,
    worker_id: str = "worker-1",
) -> None:
    with connect(state_root) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO workers VALUES(?,?,?,?)",
            (worker_id, "interactive-agent", "[]", NOW),
        )
        connection.execute(
            """
            INSERT INTO runs(
                run_id, task_id, worker_id, attempt, state, task_sha256, plan_sha256,
                envelope_json, envelope_sha256, workspace_path, workspace_branch,
                created_at, updated_at, heartbeat_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                task_id,
                worker_id,
                1,
                state,
                "sha",
                "plan",
                "{}",
                "sha",
                workspace_path,
                branch,
                NOW,
                NOW,
                NOW,
            ),
        )


def add_task_status(
    state_root: Path, task_id: str, state: str, task_sha256: str = "wrong"
) -> None:
    with connect(state_root) as connection:
        connection.execute(
            "INSERT INTO task_status(task_id, task_sha256, plan_sha256, state, updated_at)"
            " VALUES(?,?,?,?,?)",
            (task_id, task_sha256, "", state, NOW),
        )


def github_observation(
    task_id: str | None,
    *,
    number: int = 7,
    state: str = "OPEN",
    checks_summary: str = "ci_passed",
    observed_at: str = NOW,
    healthy: bool = True,
    blocked_reason: str | None = None,
    extra_pull_requests: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    pull_requests: list[dict[str, object]] = []
    if task_id is not None:
        pull_requests.append(
            {
                "repository": "heimgewebe/bureau",
                "number": number,
                "url": f"https://github.com/heimgewebe/bureau/pull/{number}",
                "title": "Example",
                "state": state,
                "is_draft": False,
                "head_ref": "feat/example",
                "head_sha": "a" * 40,
                "base_ref": "main",
                "merge_state": "CLEAN",
                "review_decision": "",
                "review_blocked": False,
                "checks": {"summary": checks_summary, "items": []},
                "updated_at": observed_at,
                "observed_at": observed_at,
                "binding": "bureau_task_marker",
                "confidence": 0.95,
                "task_id": task_id,
                "run_id": None,
                "ambiguous_reason": None,
                "notes": [],
            }
        )
    pull_requests.extend(extra_pull_requests or [])
    return {
        "schema_version": 1,
        "source": "github",
        "repository": "heimgewebe/bureau",
        "observed_at": observed_at,
        "healthy": healthy,
        "blocked_reason": blocked_reason,
        "notes": [],
        "pull_requests": pull_requests,
        "does_not_establish": ["task_completion", "merge_readiness"],
    }


def project(root: Path, **kwargs):
    kwargs.setdefault("state_root", root / "state")
    kwargs.setdefault("now", NOW)
    return status_projection(root, **kwargs)


def task_entry(projection: dict, task_id: str) -> dict:
    return next(item for item in projection["tasks"] if item["task_id"] == task_id)


def test_status_projection_exposes_ai_authority_boundary(registry_factory) -> None:
    root = registry_factory()
    projection = project(root, state_root=root / "no-state")
    assert projection["authority_boundary"]["ai"] == AI_AUTHORITY_BOUNDARY
    assert projection["authority_boundary"]["ai"]["core_policy"] == "deterministic_only"
    assert projection["authority_boundary"]["ai"]["llm_outputs"] == "advisory_only"
    assert "queue_mutation" in projection["authority_boundary"]["ai"]["forbidden_effects"]
    assert "task_verification" in projection["authority_boundary"]["ai"]["forbidden_effects"]
    projection["authority_boundary"]["ai"]["forbidden_effects"].append("mutated")
    assert "mutated" not in AI_AUTHORITY_BOUNDARY["forbidden_effects"]
    assert "ai_authority" in projection["does_not_establish"]


def test_registry_only_projection_keeps_unknowns_visible(registry_factory) -> None:
    root = registry_factory()
    projection = project(root, state_root=root / "no-state")
    assert projection["schema_version"] == STATUS_PROJECTION_SCHEMA_VERSION
    assert projection["state_store"]["available"] is False
    entry = task_entry(projection, TASK_1)
    assert entry["registry_state"] == "ready"
    assert entry["effective_state"] == "ready"
    assert entry["queue_lane"] == "now"
    assert entry["active_run"] is None
    assert entry["workspace"] is None
    assert entry["receipts"] == []
    assert entry["github"] is None
    assert "runtime-state-unavailable" in entry["unknowns"]
    assert "github-not-observed" in entry["unknowns"]
    assert projection["healthy"] is True


def test_active_run_and_workspace_are_projected(registry_factory) -> None:
    root = registry_factory()
    state_root = make_state(root)
    add_run(
        state_root,
        "BUR-RUN-1",
        TASK_1,
        branch="task/bur-test-001-t001",
        workspace_path=str(root / "ws"),
    )
    with connect(state_root) as connection:
        connection.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "BUR-RUN-1",
                str(root),
                str(root / "ws"),
                "task/bur-test-001-t001",
                "c" * 40,
                "active",
                NOW,
                NOW,
                None,
            ),
        )
    projection = project(root)
    entry = task_entry(projection, TASK_1)
    assert entry["active_run"]["run_id"] == "BUR-RUN-1"
    assert entry["active_run"]["worker"] == "worker-1"
    assert entry["active_run"]["state"] == "running"
    assert entry["workspace"]["branch"] == "task/bur-test-001-t001"
    assert entry["workspace"]["state"] == "active"
    other = task_entry(projection, TASK_2)
    assert other["active_run"] is None


def test_receipt_is_shown_but_not_read_as_completion(registry_factory) -> None:
    root = registry_factory()
    state_root = make_state(root)
    add_run(state_root, "BUR-RUN-2", TASK_1, state="succeeded")
    with connect(state_root) as connection:
        connection.execute(
            "INSERT INTO receipts VALUES(?,?,?,?)",
            ("BUR-RUN-2", "{}", "d" * 64, NOW),
        )
    projection = project(root)
    entry = task_entry(projection, TASK_1)
    assert entry["receipts"][0]["receipt_sha256"] == "d" * 64
    assert "not task completion" in entry["receipts"][0]["establishes"]
    assert entry["effective_state"] == "ready"
    assert entry["registry_state"] == "ready"


def test_github_observation_is_bound_to_task(registry_factory) -> None:
    root = registry_factory()
    projection = project(root, github=github_observation(TASK_1))
    entry = task_entry(projection, TASK_1)
    assert entry["github"]["number"] == 7
    assert entry["github"]["binding"] == "bureau_task_marker"
    assert entry["github"]["confidence"] == 0.95
    assert projection["github_observation"]["observed"] is True
    assert projection["github_observation"]["healthy"] is True
    other = task_entry(projection, TASK_2)
    assert other["github"] is None


def test_ci_unknown_stays_unknown(registry_factory) -> None:
    root = registry_factory()
    projection = project(
        root, github=github_observation(TASK_1, checks_summary="ci_unknown")
    )
    entry = task_entry(projection, TASK_1)
    assert entry["github"]["checks"]["summary"] == "ci_unknown"
    assert "ci-unknown" in entry["unknowns"]
    assert projection["healthy"] is True


def test_blocked_github_observation_stays_blocked(registry_factory) -> None:
    root = registry_factory()
    projection = project(
        root,
        github=github_observation(None, healthy=False, blocked_reason="gh unavailable"),
    )
    entry = task_entry(projection, TASK_1)
    assert any("github-observation-blocked" in reason for reason in entry["blocked_reasons"])
    assert projection["healthy"] is False
    assert projection["github_observation"]["blocked_reason"] == "gh unavailable"


def test_stale_verification_overlay_stays_stale(registry_factory) -> None:
    root = registry_factory()
    state_root = make_state(root)
    add_task_status(state_root, TASK_1, "verified", task_sha256="stale-sha")
    projection = project(root)
    entry = task_entry(projection, TASK_1)
    assert entry["effective_state"] == "stale"
    assert "verification-stale" in entry["stale_reasons"]
    assert projection["healthy"] is False


def test_stale_github_observation_stays_visible(registry_factory) -> None:
    root = registry_factory()
    projection = project(
        root,
        github=github_observation(TASK_1, observed_at="2026-07-07T09:00:00Z"),
        github_max_age_seconds=600,
    )
    entry = task_entry(projection, TASK_1)
    assert "github-observation-stale" in entry["stale_reasons"]
    assert projection["github_observation"]["stale"] is True
    assert projection["healthy"] is False


def test_merged_pr_without_bureau_evidence_is_not_verified(registry_factory) -> None:
    root = registry_factory()
    projection = project(root, github=github_observation(TASK_1, state="MERGED"))
    entry = task_entry(projection, TASK_1)
    assert entry["effective_state"] == "ready"
    codes = [finding["code"] for finding in entry["findings"]]
    assert "merged-pr-without-bureau-verification" in codes


def test_ambiguous_github_binding_is_a_hard_finding(registry_factory) -> None:
    root = registry_factory()
    second = dict(github_observation(TASK_1, number=8)["pull_requests"][0])
    projection = project(
        root, github=github_observation(TASK_1, extra_pull_requests=[second])
    )
    entry = task_entry(projection, TASK_1)
    assert entry["github"]["binding"] == "ambiguous"
    assert entry["github"]["candidates"] == [7, 8]
    codes = [finding["code"] for finding in entry["findings"]]
    assert "github-binding-ambiguous" in codes
    assert projection["healthy"] is False


def test_unbound_unhealthy_github_binding_is_top_level_blocker(registry_factory) -> None:
    root = registry_factory()
    github = github_observation(None)
    github["binding_healthy"] = False
    github["hard_findings"] = [
        {
            "severity": "blocker",
            "code": "ambiguous-github-binding",
            "message": "multiple-bureau-task-markers",
            "number": 9,
            "task_id": None,
        }
    ]
    github["pull_requests"] = [
        {
            "number": 9,
            "binding": "ambiguous",
            "confidence": None,
            "ambiguous_reason": "multiple-bureau-task-markers",
            "task_id": None,
            "observed_at": NOW,
        }
    ]

    projection = project(root, github=github)

    assert projection["healthy"] is False
    assert projection["github_observation"]["healthy"] is True
    assert projection["github_observation"]["binding_healthy"] is False
    assert (
        projection["github_observation"]["hard_findings"][0]["code"]
        == "ambiguous-github-binding"
    )
    assert projection["findings"][0]["code"] == "github-binding-unhealthy"
    entry = task_entry(projection, TASK_1)
    assert entry["github"] is None


def test_task_priority_without_queue_entry_is_reported_as_advisory(registry_factory) -> None:
    root = registry_factory()
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue["lanes"]["now"] = [tid for tid in queue["lanes"]["now"] if tid != TASK_1]
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    projection = project(root)
    entry = task_entry(projection, TASK_1)
    assert entry["queue_lane"] is None
    finding = next(
        item for item in entry["findings"] if item["code"] == "task-priority-not-queued"
    )
    assert finding["severity"] == "warning"
    assert finding["declared_lane"] == "now"
    assert finding["queue_canonical"] is True
    assert projection["healthy"] is True


def test_projection_is_stable_and_read_only(registry_factory) -> None:
    root = registry_factory()
    state_root = make_state(root)
    before = (state_root / "bureau.sqlite3").read_bytes()
    first = project(root, github=github_observation(TASK_1))
    second = project(root, github=github_observation(TASK_1))
    assert first == second
    assert (state_root / "bureau.sqlite3").read_bytes() == before
    assert set(PROJECTION_DOES_NOT_ESTABLISH) == set(first["does_not_establish"])
    assert {
        "schema_version",
        "generated_at",
        "root",
        "state_root",
        "state_store",
        "github_observation",
        "healthy",
        "findings",
        "tasks",
        "does_not_establish",
    } <= set(first)
    assert [item["task_id"] for item in first["tasks"]] == sorted(
        item["task_id"] for item in first["tasks"]
    )


def test_cli_status_projection_with_skip_github(registry_factory, capsys) -> None:
    root = registry_factory()
    code = main(
        [
            "--root",
            str(root),
            "--state-root",
            str(root / "no-state"),
            "--json",
            "status-projection",
            "--skip-github",
        ]
    )
    assert code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["github_observation"]["observed"] is False
    assert value["schema_version"] == STATUS_PROJECTION_SCHEMA_VERSION


def test_cli_status_projection_with_observation_file(
    registry_factory, tmp_path: Path, capsys
) -> None:
    root = registry_factory()
    observations = tmp_path / "observations.json"
    observations.write_text(
        json.dumps(github_observation(TASK_1)), encoding="utf-8"
    )
    code = main(
        [
            "--root",
            str(root),
            "--state-root",
            str(root / "no-state"),
            "--json",
            "status-projection",
            "--github-observations",
            str(observations),
            "--github-max-age",
            "999999999",
        ]
    )
    assert code == 0
    value = json.loads(capsys.readouterr().out)
    entry = next(item for item in value["tasks"] if item["task_id"] == TASK_1)
    assert entry["github"]["number"] == 7


def test_cli_github_observe_blocked_exits_nonzero(
    registry_factory, tmp_path: Path, capsys, monkeypatch
) -> None:
    root = registry_factory()
    monkeypatch.setenv("BUREAU_GH_BIN", str(tmp_path / "missing-gh"))
    code = main(
        [
            "--root",
            str(root),
            "--state-root",
            str(root / "no-state"),
            "--json",
            "github-observe",
            "--repo",
            "heimgewebe/bureau",
        ]
    )
    assert code == 1
    value = json.loads(capsys.readouterr().out)
    assert value["healthy"] is False
    assert value["pull_requests"] == []


def test_projection_exposes_ai_authority_boundary(registry_factory) -> None:
    root = registry_factory()
    projection = project(root)

    assert projection["authority_boundary"]["ai"] == AI_AUTHORITY_BOUNDARY
    assert projection["authority_boundary"]["ai"]["core_policy"] == "deterministic_only"
    assert projection["authority_boundary"]["ai"]["llm_outputs"] == "advisory_only"
    assert "ai_authority" in projection["does_not_establish"]
    projection["authority_boundary"]["ai"]["forbidden_effects"].append("mutated")
    assert "mutated" not in AI_AUTHORITY_BOUNDARY["forbidden_effects"]


def test_projection_includes_repository_balls_and_next_actions(
    registry_factory, tmp_path
) -> None:
    root = registry_factory(2, mode="write")
    state_root = make_state(root)
    add_run(state_root, "BUR-RUN-A", TASK_1, state="running")

    projection = project(root)

    alpha = projection["repository_balls"]["repo.alpha"]
    beta = projection["repository_balls"]["repo.beta"]
    assert alpha["status"] == "active"
    assert alpha["current_ball"]["task_id"] == TASK_1
    assert beta["status"] == "ready"
    assert beta["current_ball"]["task_id"] == TASK_2
    assert {action["action"] for action in projection["next_actions"]} >= {"claim-task"}


def test_projection_repository_ball_ambiguity_is_actionable(
    registry_factory, tmp_path
) -> None:
    root = registry_factory(3, mode="read")
    state_root = make_state(root)
    add_run(state_root, "BUR-RUN-A1", TASK_1, state="running", worker_id="worker-a1")
    add_run(
        state_root,
        "BUR-RUN-A2",
        "BUR-TEST-001-T003",
        state="running",
        worker_id="worker-a2",
    )

    projection = project(root)

    alpha = projection["repository_balls"]["repo.alpha"]
    assert alpha["status"] == "ambiguous"
    assert alpha["findings"][0]["code"] == "multiple-active-balls-for-repository"
    assert projection["next_actions"][0]["action"] == "reconcile-active-repository-balls"
    assert projection["next_actions"][0]["repository"] == "repo.alpha"
    assert projection["healthy"] is False
