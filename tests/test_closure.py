from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bureau.closure import (
    AGENT_BRIEF_REQUIRED_FIELDS,
    RepositorySource,
    inventory_existing_work,
    is_canonical_bureau_task_id,
    load_canonical_task_states,
    merge_lanes,
    run_closure_cycle,
    select_lanes_for_plan_with_evidence,
    validate_brief,
)

UNBOUND_ACTION = "bind to canonical Bureau task before dispatch"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    (repo / "README.md").write_text("main\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "switch", "-c", "feat/example")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feature")
    git(repo, "switch", "main")
    return repo


def write_source_registry(tmp_path: Path, repo: Path) -> Path:
    registry = tmp_path / "source-registry.json"
    registry.write_text(
        json.dumps(
            {
                "repositories": [
                    {"name": "repo", "root": str(repo), "source_id": "repo:repo", "enabled": True}
                ]
            }
        ),
        encoding="utf-8",
    )
    return registry


def lane(
    lane_id: str,
    state: str,
    task_id: str | None,
    *,
    repo: str = "/tmp/repo",
    repo_name: str = "repo",
    branch: str = "feat/x",
    finishability: float = 0.5,
) -> dict[str, object]:
    return {
        "lane_id": lane_id,
        "repo": repo,
        "repo_name": repo_name,
        "branch": branch,
        "state": state,
        "task_id": task_id,
        "finishability": finishability,
    }


def test_inventory_finds_unmerged_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    source = RepositorySource("repo", repo, "repo:repo")
    inventory = inventory_existing_work([source])
    assert inventory["candidate_count"] == 1
    candidate = inventory["candidates"][0]
    assert candidate["kind"] == "branch"
    assert candidate["branch"] == "feat/example"
    assert candidate["proposed_state"] == "planned"
    assert candidate["fingerprint"]


def test_run_rejects_unbound_lane_and_records_evidence(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    registry = write_source_registry(tmp_path, repo)
    monkeypatch.setenv("BUREAU_DISCOVERY_REGISTRY", str(registry))
    plan = run_closure_cycle(state_root=tmp_path / "closure")
    assert plan["selected_lane_count"] == 0
    assert plan["briefs"] == []
    assert plan["unbound_selected_rejected_count"] == 1
    rejected = plan["rejected_unbound_lanes"][0]
    assert rejected["branch"] == "feat/example"
    assert rejected["state"] == "planned"
    assert rejected["reason"] == "missing_canonical_bureau_task_id"
    lanes = json.loads((tmp_path / "closure/lanes.json").read_text(encoding="utf-8"))
    assert lanes["lanes"][0]["next_action"] == UNBOUND_ACTION


def test_run_selects_bound_lane_and_generates_brief(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    registry = write_source_registry(tmp_path, repo)
    monkeypatch.setenv("BUREAU_DISCOVERY_REGISTRY", str(registry))
    source = RepositorySource("repo", repo, "repo:repo")
    lanes = merge_lanes(inventory_existing_work([source]))
    lanes["lanes"][0]["task_id"] = "BUR-2026-001-T999"
    state_root = tmp_path / "closure"
    state_root.mkdir()
    (state_root / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
    plan = run_closure_cycle(state_root=state_root)
    assert plan["selected_lane_count"] == 1
    assert plan["unbound_selected_rejected_count"] == 0
    assert plan["canonical_task_bound_count"] == 1
    assert plan["selected_lanes"][0]["task_id"] == "BUR-2026-001-T999"
    brief_path = Path(plan["briefs"][0]["path"])
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    assert not validate_brief(brief)
    for field in AGENT_BRIEF_REQUIRED_FIELDS:
        assert field in brief



def test_verified_canonical_task_is_not_selected_again(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    source = RepositorySource("repo", repo, "repo:repo")
    lanes = merge_lanes(inventory_existing_work([source]))
    task_id = "BUR-2026-001-T999"
    lanes["lanes"][0]["task_id"] = task_id
    state_root = tmp_path / "closure"
    state_root.mkdir()
    (state_root / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
    bureau_root = tmp_path / "bureau"
    task_dir = bureau_root / "registry/tasks"
    task_dir.mkdir(parents=True)
    (task_dir / f"{task_id}.json").write_text(
        json.dumps({"id": task_id, "state": "verified"}), encoding="utf-8"
    )
    registry = write_source_registry(tmp_path, repo)
    monkeypatch.setenv("BUREAU_DISCOVERY_REGISTRY", str(registry))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(bureau_root))

    plan = run_closure_cycle(state_root=state_root)

    assert plan["canonical_task_state_count"] == 1
    assert plan["selected_lane_count"] == 0
    assert plan["selected_lanes"] == []
    updated = json.loads((state_root / "lanes.json").read_text(encoding="utf-8"))
    assert updated["lanes"][0]["state"] == "verified"
    assert updated["lanes"][0]["metadata"]["canonical_task_state"] == "verified"


def test_load_canonical_task_states_reads_registry_root(tmp_path: Path) -> None:
    task_dir = tmp_path / "registry/tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "BUR-2026-001-T001.json").write_text(
        json.dumps({"id": "BUR-2026-001-T001", "state": "verified"}),
        encoding="utf-8",
    )

    assert load_canonical_task_states(tmp_path) == {"BUR-2026-001-T001": "verified"}

def test_selection_rejects_grabowski_task_id_for_ci_failed_lane() -> None:
    values = [lane("lane-1", "ci_failed", "a4d2e0bc80f749ebb4482961")]
    selection = select_lanes_for_plan_with_evidence(values)
    assert selection["selected_lanes"] == []
    assert selection["unbound_selected_rejected_count"] == 1
    assert selection["rejected_unbound_lanes"][0]["task_id"] == "a4d2e0bc80f749ebb4482961"
    assert values[0]["next_action"] == UNBOUND_ACTION


def test_selection_selects_bound_planned_lane() -> None:
    selection = select_lanes_for_plan_with_evidence(
        [lane("lane-1", "planned", "BUR-2026-001-T999")]
    )
    assert [item["lane_id"] for item in selection["selected_lanes"]] == ["lane-1"]
    assert selection["canonical_task_bound_count"] == 1
    assert selection["unbound_selected_rejected_count"] == 0


def test_selection_rejects_unbound_merge_candidate() -> None:
    selection = select_lanes_for_plan_with_evidence([lane("lane-1", "merge_candidate", None)])
    assert selection["selected_lanes"] == []
    assert selection["rejected_unbound_lanes"] == [
        {
            "lane_id": "lane-1",
            "repo_name": "repo",
            "branch": "feat/x",
            "state": "merge_candidate",
            "task_id": None,
            "reason": "missing_canonical_bureau_task_id",
        }
    ]


def test_selection_respects_max_selected_lanes_and_terminal_exclusions() -> None:
    values = [lane(f"lane-{idx}", "discovered", None, branch=f"feat/{idx}") for idx in range(6)]
    values.extend([lane("closed", "closed", "BUR-1"), lane("obsolete", "obsolete", "BUR-2")])
    selection = select_lanes_for_plan_with_evidence(values, limits={"max_selected_lanes": 4})
    assert len(selection["selected_lanes"]) == 4
    assert {item["state"] for item in selection["selected_lanes"]} == {"discovered"}


def test_canonical_task_id_detection() -> None:
    assert is_canonical_bureau_task_id("BUR-2026-001-T999")
    assert not is_canonical_bureau_task_id("a4d2e0bc80f749ebb4482961")
    assert not is_canonical_bureau_task_id(None)


def test_manual_intent_marks_lane(tmp_path: Path) -> None:
    inventory = {
        "candidates": [
            {
                "kind": "branch",
                "repo": "/tmp/repo",
                "repo_name": "repo",
                "branch": "feat/x",
                "fingerprint": "abc",
                "proposed_state": "planned",
                "finishability": 0.5,
            }
        ]
    }
    lanes = merge_lanes(inventory, manual_intents=[{"target": "feat/x", "priority": "high"}])
    assert lanes["lanes"][0]["manual_priority"] == "high"
