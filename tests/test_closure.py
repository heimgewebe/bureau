from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bureau.closure import (
    AGENT_BRIEF_REQUIRED_FIELDS,
    RepositorySource,
    inventory_existing_work,
    merge_lanes,
    run_closure_cycle,
    validate_brief,
)


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


def test_lane_merge_and_brief_generation(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
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
    monkeypatch.setenv("BUREAU_DISCOVERY_REGISTRY", str(registry))
    plan = run_closure_cycle(state_root=tmp_path / "closure")
    assert plan["selected_lane_count"] == 1
    brief_path = Path(plan["briefs"][0]["path"])
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    assert not validate_brief(brief)
    for field in AGENT_BRIEF_REQUIRED_FIELDS:
        assert field in brief


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
