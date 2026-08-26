from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bureau.core import Dispatcher, NoEligibleTask, Registry, StateStore
from bureau.frontier import build_frontier_projection
from bureau.v2 import _complete_run_after_typed_evaluation as complete_run
from bureau.v2 import task_revision_sha256


def _git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _init_clean_origin_main(root: Path) -> str:
    _git_output(root, "init", "-b", "main")
    _git_output(root, "config", "user.email", "bureau-test@example.invalid")
    _git_output(root, "config", "user.name", "Bureau Test")
    _git_output(root, "add", ".")
    _git_output(root, "commit", "-m", "initial")
    head = _git_output(root, "rev-parse", "HEAD")
    _git_output(root, "update-ref", "refs/remotes/origin/main", head)
    return head


def _setup(root: Path, tmp_path: Path, monkeypatch):
    state_root = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3")
    return registry, store


def _complete_then_revise_verified_task(
    root: Path,
    tmp_path: Path,
    monkeypatch,
    *,
    revised_state: str = "ready",
):
    registry, store = _setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("initial-worker", ("repository",))["run"]
    complete_run(
        registry,
        store,
        run["run_id"],
        {"proof": {"result": "passed"}},
    )
    task_id = run["task_id"]

    with store.connect() as connection:
        status_before = dict(
            connection.execute(
                "SELECT task_id,state,receipt_sha256,task_sha256,plan_sha256 "
                "FROM task_status WHERE task_id=?",
                (task_id,),
            ).fetchone()
        )

    current = store.task_spec(task_id)
    assert current is not None
    revised = json.loads(json.dumps(current["spec"]))
    revised["title"] = "Revised after verified receipt"
    revised["state"] = revised_state
    mutation = store.put_task_spec(
        revised,
        idempotency_key=f"reverification-deadlock-{revised_state}",
        expected_revision=current["revision"],
        source="test-reverification-deadlock",
    )
    assert mutation["spec_sha256"]
    assert task_revision_sha256(revised) != status_before["task_sha256"]
    return registry, store, task_id, status_before


def test_ready_revision_after_verified_receipt_is_self_reverification_candidate(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _init_clean_origin_main(root)
    registry, store, task_id, status_before = _complete_then_revise_verified_task(
        root, tmp_path, monkeypatch
    )
    dispatcher = Dispatcher(registry, store)

    with store.connect() as connection:
        overlays = store.overlays(connection, dispatcher.registry)
    assert overlays[task_id] == "stale"

    item = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == task_id
    )
    assert item["effective_state"] == "stale"
    assert item["eligible"] is True
    assert "state is stale" not in item["reasons"]

    intent = dispatcher.claim_intent(
        "reverification-worker",
        ("repository",),
        task_id=task_id,
    )
    assert intent["status"] == "claim-intent"
    assert intent["intent"]["task_id"] == task_id
    assert intent["intent"]["required_resource_keys"] == []

    claimed = dispatcher.commit_claim_intent(intent["intent"], None)
    assert claimed["status"] == "claimed"
    assert claimed["run"]["task_id"] == task_id

    with store.connect() as connection:
        status_after = dict(
            connection.execute(
                "SELECT task_id,state,receipt_sha256,task_sha256,plan_sha256 "
                "FROM task_status WHERE task_id=?",
                (task_id,),
            ).fetchone()
        )
    assert status_after == status_before


def test_frontier_projects_ready_stale_task_as_reverification_candidate(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, task_id, _status_before = _complete_then_revise_verified_task(
        root, tmp_path, monkeypatch
    )

    projection = build_frontier_projection(
        registry,
        store,
        capabilities={"repository"},
        check_runtime=False,
    )
    card = next(
        card
        for lane in projection["lanes"].values()
        for card in lane
        if card["task_id"] == task_id
    )

    assert card["effective_state"] == "stale"
    assert card["reverification_candidate"] is True
    assert card["structurally_eligible"] is True
    assert card["claim_eligible"] is True
    assert card["projected_lane"] == "now"


def test_nonready_revision_after_verified_receipt_stays_unclaimable(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _init_clean_origin_main(root)
    registry, store, task_id, _status_before = _complete_then_revise_verified_task(
        root, tmp_path, monkeypatch, revised_state="planned"
    )
    dispatcher = Dispatcher(registry, store)

    with store.connect() as connection:
        overlays = store.overlays(connection, dispatcher.registry)
    assert overlays[task_id] == "stale"

    with pytest.raises(NoEligibleTask, match="state is stale"):
        dispatcher.claim_intent(
            "nonready-reverification-worker",
            ("repository",),
            task_id=task_id,
        )


def test_git_bootstrap_task_revision_drift_stays_global_and_task_blocking(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _init_clean_origin_main(root)
    registry, store = _setup(root, tmp_path, monkeypatch)
    task_id = next(iter(registry.tasks))
    assert store.task_spec(task_id) is None

    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("bootstrap-worker", ("repository",))["run"]
    complete_run(
        registry,
        store,
        run["run_id"],
        {"proof": {"result": "passed"}},
    )
    assert store.task_spec(task_id) is None

    task_path = root / f"registry/tasks/{task_id}.json"
    task = json.loads(task_path.read_text())
    task["title"] = "Git bootstrap revision after verified receipt"
    task["state"] = "ready"
    task_path.write_text(json.dumps(task))
    _git_output(root, "add", ".")
    _git_output(root, "commit", "-m", "revise git bootstrap task")
    _git_output(
        root,
        "update-ref",
        "refs/remotes/origin/main",
        _git_output(root, "rev-parse", "HEAD"),
    )

    changed = Registry.load(root)
    task_dispatcher = Dispatcher(changed, store)
    assert task_dispatcher.task_authority.get("kind") != "bureau-state-store-task-specs"
    with store.connect() as connection:
        overlays = store.overlays(connection, task_dispatcher.registry)
    assert overlays[task_id] == "stale"
    item = next(
        item
        for item in task_dispatcher.frontier({"repository"})
        if item["task_id"] == task_id
    )
    assert item["effective_state"] == "stale"
    assert item["eligible"] is False
    assert "state is stale" in item["reasons"]

    gated = Dispatcher(changed, store, enforce_runtime_gate=True)
    runtime_truth = gated._runtime_execution_truth()
    assert runtime_truth["runtime_status"] == "blocked"
    assert runtime_truth["execution_blocked"] is True
    assert "receipt-drift" in runtime_truth["blocker_codes"]


def test_plan_only_receipt_drift_stays_global_and_task_blocking(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _init_clean_origin_main(root)
    registry, store = _setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("initial-worker", ("repository",))["run"]
    complete_run(
        registry,
        store,
        run["run_id"],
        {"proof": {"result": "passed"}},
    )
    task_id = run["task_id"]

    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {
        "repository": "test",
        "path": "plan.md",
        "commit": "1" * 40,
        "document_sha256": "2" * 64,
    }
    initiative_path.write_text(json.dumps(initiative))
    _git_output(root, "add", ".")
    _git_output(root, "commit", "-m", "change plan after verification")
    _git_output(
        root,
        "update-ref",
        "refs/remotes/origin/main",
        _git_output(root, "rev-parse", "HEAD"),
    )

    changed = Registry.load(root)
    task_dispatcher = Dispatcher(changed, store)
    with store.connect() as connection:
        overlays = store.overlays(connection, task_dispatcher.registry)
    assert overlays[task_id] == "stale"
    item = next(
        item
        for item in task_dispatcher.frontier({"repository"})
        if item["task_id"] == task_id
    )
    assert item["effective_state"] == "stale"
    assert item["eligible"] is False
    assert "state is stale" in item["reasons"]
    with pytest.raises(NoEligibleTask, match="state is stale"):
        task_dispatcher.claim_intent(
            "plan-drift-worker",
            ("repository",),
            task_id=task_id,
        )

    gated = Dispatcher(changed, store, enforce_runtime_gate=True)
    runtime_truth = gated._runtime_execution_truth()
    assert runtime_truth["runtime_status"] == "blocked"
    assert runtime_truth["execution_blocked"] is True
    assert "receipt-drift" in runtime_truth["blocker_codes"]


def test_receipt_drift_does_not_globally_block_independent_ready_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    _init_clean_origin_main(root)
    registry, store, stale_task_id, _status_before = _complete_then_revise_verified_task(
        root, tmp_path, monkeypatch
    )
    independent_task_id = next(
        task_id for task_id in registry.tasks if task_id != stale_task_id
    )
    dispatcher = Dispatcher(registry, store, enforce_runtime_gate=True)

    runtime_truth = dispatcher._runtime_execution_truth()
    assert runtime_truth["runtime_status"] == "blocked"
    assert runtime_truth["execution_blocked"] is False
    assert "receipt-drift" not in runtime_truth["blocker_codes"]

    intent = dispatcher.claim_intent(
        "independent-worker",
        ("repository",),
        task_id=independent_task_id,
    )
    assert intent["status"] == "claim-intent"
    assert intent["intent"]["task_id"] == independent_task_id
