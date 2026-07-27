from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bureau import legacy
from bureau.core import Dispatcher, NoEligibleTask, Registry, StateError, StateStore
from bureau.v2 import coordinated_claim_intent_sha256


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _init_clean_main(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "bureau-test@example.invalid")
    _git(root, "config", "user.name", "Bureau Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", head)


def _write_reviewed_plan(path: Path, task: dict) -> None:
    proposal: dict = {
        "schema_version": 1,
        "kind": "bureau_operator_task_proposal",
        "command": "operator-task-propose",
        "task_id": task["id"],
        "target_path": f"registry/tasks/{task['id']}.json",
        "task_json": task,
        "task_json_sha256": legacy.sha256_json(task),
        "unresolved_fields": [],
        "publication": {
            "action_class": "registry_mutation",
            "required_level": "reviewed_plan",
            "queue_mutated": False,
        },
    }
    unsigned = {
        key: value
        for key, value in proposal.items()
        if key not in {"proposal_sha256", "review"}
    }
    proposal_sha256 = legacy.sha256_json(unsigned)
    proposal["proposal_sha256"] = proposal_sha256
    proposal["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "evidence-bound-test-reviewer",
        "reviewed_at": "2026-07-27T20:00:00Z",
        "reviewed_proposal_sha256": proposal_sha256,
    }
    path.write_text(
        json.dumps(proposal, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_broad_task(root: Path, plan_path: Path) -> str:
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["execution"]["policy"] = "review-before-effect"
    task["execution"]["grabowski_resources"] = [
        "repo:/home/alex/repos/bureau"
    ]
    task["execution"]["broad_bureau_scope_exception"] = {
        "justification": (
            "One atomic repository-format migration covers all tracked "
            "Bureau files."
        ),
        "effect_boundaries": ["all tracked Bureau files"],
        "approval": {
            "action_class": "registry_mutation",
            "required_level": "reviewed_plan",
        },
        "reviewed_plan_path": str(plan_path),
    }
    for claim in task["claims"]:
        claim["isolation"] = "none"
    task_path.write_text(
        json.dumps(task, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_reviewed_plan(plan_path, task)
    _init_clean_main(root)
    return str(task["id"])


def _dispatcher(
    root: Path,
    state_path: Path,
) -> tuple[StateStore, Dispatcher]:
    registry = Registry.load(root)
    store = StateStore(state_path)
    return store, Dispatcher(registry, store)


def test_legacy_claim_surface_cannot_bypass_broad_scope_review(
    registry_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "0")
    root = registry_factory(1, mode="write")
    task_id = _prepare_broad_task(
        root,
        tmp_path / "reviewed-plan.json",
    )
    store, dispatcher = _dispatcher(
        root,
        tmp_path / "state.sqlite3",
    )

    with pytest.raises(
        NoEligibleTask,
        match="digest-bound reviewed plan evidence",
    ):
        dispatcher.claim_next("legacy-worker", ("repository",))

    assert store.list_runs() == []
    assert dispatcher.registry.tasks[task_id].state == "ready"


def test_coordinated_claim_binds_reviewed_plan_before_lease_validation(
    registry_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "0")
    root = registry_factory(1, mode="write")
    plan_path = tmp_path / "reviewed-plan.json"
    task_id = _prepare_broad_task(root, plan_path)
    store, dispatcher = _dispatcher(
        root,
        tmp_path / "state.sqlite3",
    )

    result = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="test operator approval",
    )
    intent = result["intent"]
    evidence = intent["operator_approval"]["broad_scope_review"]

    assert result["ready_supply"]["broad_scope_review_bound"] is True
    assert evidence["task_id"] == task_id
    assert evidence["task_sha256"] == dispatcher.registry.tasks[task_id].sha256
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert evidence["proposal_sha256"] == plan["proposal_sha256"]
    assert evidence["approval"]["allowed"] is True

    with pytest.raises(StateError, match="lease binding is required"):
        dispatcher.commit_claim_intent(intent, None)
    assert store.list_runs() == []


def test_operator_only_intent_cannot_impersonate_broad_scope_review(
    registry_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "0")
    root = registry_factory(1, mode="write")
    task_id = _prepare_broad_task(
        root,
        tmp_path / "reviewed-plan.json",
    )
    store, dispatcher = _dispatcher(
        root,
        tmp_path / "state.sqlite3",
    )
    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
    )["intent"]
    del intent["operator_approval"]["broad_scope_review"]
    intent["intent_sha256"] = coordinated_claim_intent_sha256(intent)

    with pytest.raises(
        StateError,
        match="matching digest-bound reviewed plan evidence",
    ):
        dispatcher.commit_claim_intent(intent, None)

    assert store.list_runs() == []


def test_reviewed_plan_drift_blocks_claim_before_lease_validation(
    registry_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "0")
    root = registry_factory(1, mode="write")
    plan_path = tmp_path / "reviewed-plan.json"
    task_id = _prepare_broad_task(root, plan_path)
    store, dispatcher = _dispatcher(
        root,
        tmp_path / "state.sqlite3",
    )
    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
    )["intent"]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["review"]["reviewer"] = "tampered-reviewer"
    plan_path.write_text(
        json.dumps(plan, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        StateError,
        match="matching digest-bound reviewed plan evidence",
    ):
        dispatcher.commit_claim_intent(intent, None)

    assert store.list_runs() == []
