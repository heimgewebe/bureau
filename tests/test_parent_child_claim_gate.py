from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bureau import legacy
from bureau.broad_scope_dispatcher import Dispatcher as BroadScopeDispatcher
from bureau.core import Dispatcher, NoEligibleTask, Registry, StateStore, ValidationError
from bureau.v2 import plan_sha256, task_revision_sha256

PARENT_ID = "BUR-TEST-001-T001"
PLANNED_CHILD_ID = "BUR-TEST-001-T002"
BLOCKED_CHILD_ID = "BUR-TEST-001-T003"
READY_CHILD_ID = "BUR-TEST-001-T004"
VERIFIED_CHILD_ID = "BUR-TEST-001-T005"
CANCELLED_CHILD_ID = "BUR-TEST-001-T006"
SUPERSEDED_CHILD_ID = "BUR-TEST-001-T007"
BLOCKING_CHILD_IDS = [
    PLANNED_CHILD_ID,
    BLOCKED_CHILD_ID,
    READY_CHILD_ID,
]
BLOCKER_REASON = (
    "parent task blocked by nonterminal child tasks: "
    + ", ".join(BLOCKING_CHILD_IDS)
)


def _read_task(root: Path, task_id: str) -> tuple[Path, dict]:
    path = root / "registry/tasks" / f"{task_id}.json"
    return path, json.loads(path.read_text())


def _write_task(root: Path, task_id: str, task: dict) -> None:
    path = root / "registry/tasks" / f"{task_id}.json"
    path.write_text(json.dumps(task))


def _set_metadata(root: Path, task_id: str, key: str, value: object) -> None:
    _path, task = _read_task(root, task_id)
    task.setdefault("metadata", {})[key] = value
    _write_task(root, task_id, task)


def _configure_parent_child_registry(root: Path) -> None:
    states = {
        PARENT_ID: "ready",
        PLANNED_CHILD_ID: "planned",
        BLOCKED_CHILD_ID: "blocked",
        READY_CHILD_ID: "ready",
        VERIFIED_CHILD_ID: "verified",
        CANCELLED_CHILD_ID: "cancelled",
        SUPERSEDED_CHILD_ID: "superseded",
    }
    for task_id, state in states.items():
        _path, task = _read_task(root, task_id)
        task["state"] = state
        if task_id != PARENT_ID:
            task.setdefault("metadata", {})["parent_task"] = PARENT_ID
        lane = "now" if state == "ready" else "next"
        task["priority"]["lane"] = lane
        if state == "verified":
            task["metadata"]["verification"] = {
                "task_sha256": task_revision_sha256(task),
                "plan_sha256": legacy.sha256_json({}),
            }
        _write_task(root, task_id, task)

    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"] = {
        "now": [PARENT_ID, READY_CHILD_ID],
        "next": [PLANNED_CHILD_ID, BLOCKED_CHILD_ID],
        "later": [],
    }
    queue_path.write_text(json.dumps(queue))


def _v2_dispatcher(root: Path, state_root: Path) -> tuple[Registry, StateStore, Dispatcher]:
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3")
    return registry, store, Dispatcher(registry, store)


@pytest.mark.parametrize("parent_task", [7, "", "not-a-valid-task-id"])
def test_parent_task_must_be_a_nonempty_valid_task_id(
    registry_factory, parent_task
):
    root = registry_factory(2)
    _set_metadata(root, "BUR-TEST-001-T002", "parent_task", parent_task)

    with pytest.raises(
        legacy.ValidationError,
        match=r"BUR-TEST-001-T002 has invalid metadata\.parent_task",
    ):
        legacy.Registry.load(root)
    with pytest.raises(ValidationError, match=r"metadata\.parent_task"):
        Registry.load(root)


def test_parent_task_must_exist_and_cannot_be_self(registry_factory):
    missing_root = registry_factory(2)
    _set_metadata(
        missing_root,
        "BUR-TEST-001-T002",
        "parent_task",
        "BUR-TEST-001-T999",
    )
    with pytest.raises(
        ValidationError,
        match="task BUR-TEST-001-T002 has unknown parent task BUR-TEST-001-T999",
    ):
        Registry.load(missing_root)

    self_root = registry_factory(3)
    _set_metadata(self_root, PARENT_ID, "parent_task", PARENT_ID)
    with pytest.raises(
        ValidationError,
        match="task BUR-TEST-001-T001 cannot parent itself",
    ):
        Registry.load(self_root)


def test_parent_cycles_have_one_stable_bounded_diagnostic(registry_factory):
    root = registry_factory(3)
    _set_metadata(root, "BUR-TEST-001-T001", "parent_task", "BUR-TEST-001-T002")
    _set_metadata(root, "BUR-TEST-001-T002", "parent_task", "BUR-TEST-001-T003")
    _set_metadata(root, "BUR-TEST-001-T003", "parent_task", "BUR-TEST-001-T001")
    diagnostic = (
        "task parent cycle: BUR-TEST-001-T001 -> BUR-TEST-001-T002 -> "
        "BUR-TEST-001-T003 -> BUR-TEST-001-T001"
    )

    with pytest.raises(legacy.ValidationError) as legacy_error:
        legacy.Registry.load(root)
    assert str(legacy_error.value).splitlines().count(diagnostic) == 1
    with pytest.raises(ValidationError) as v2_error:
        Registry.load(root)
    assert str(v2_error.value).splitlines().count(diagnostic) == 1


def test_independently_executable_must_be_boolean(registry_factory):
    root = registry_factory(1)
    _set_metadata(root, PARENT_ID, "independently_executable", "true")

    with pytest.raises(
        legacy.ValidationError,
        match=r"metadata\.independently_executable must be boolean",
    ):
        legacy.Registry.load(root)
    with pytest.raises(ValidationError, match=r"metadata\.independently_executable"):
        Registry.load(root)


def test_projection_uses_only_explicit_parent_metadata(registry_factory):
    root = registry_factory(2)
    _path, child = _read_task(root, "BUR-TEST-001-T002")
    child["title"] = f"Child of {PARENT_ID}"
    child["depends_on"] = [PARENT_ID]
    _write_task(root, child["id"], child)

    registry = Registry.load(root)

    assert registry.parent_child_projection(PARENT_ID).child_task_ids == ()


def test_v2_selection_claim_and_task_cards_share_the_parent_blocker(
    registry_factory, tmp_path
):
    root = registry_factory(7, mode="write")
    _configure_parent_child_registry(root)
    registry_files_before = {
        path: path.read_bytes() for path in (root / "registry").rglob("*.json")
    }
    _registry, _store, dispatcher = _v2_dispatcher(root, tmp_path / "v2-state")

    frontier = dispatcher.frontier({"repository"})
    parent_frontier = next(item for item in frontier if item["task_id"] == PARENT_ID)
    assert parent_frontier["eligible"] is False
    assert parent_frontier["blocking_child_task_ids"] == BLOCKING_CHILD_IDS
    assert BLOCKER_REASON in parent_frontier["reasons"]

    explained = dispatcher.explain_next({"repository"})
    explained_parent = next(
        item for item in explained["frontier"] if item["task_id"] == PARENT_ID
    )
    assert explained["selected"]["task_id"] == READY_CHILD_ID
    assert explained_parent["blocking_child_task_ids"] == BLOCKING_CHILD_IDS
    assert BLOCKER_REASON in explained_parent["reasons"]

    what_now = dispatcher.what_now({"repository"}, limit=10)
    blocked_parent = next(
        item for item in what_now["blocked"] if item["task_id"] == PARENT_ID
    )
    assert what_now["selected"]["task_id"] == READY_CHILD_ID
    assert blocked_parent["blocking_child_task_ids"] == BLOCKING_CHILD_IDS
    assert blocked_parent["blocker_reasons"] == [BLOCKER_REASON]
    assert (
        "explicit parent-child claim gate and effective child states"
        in what_now["ranking_contract"]["eligibility_inputs"]
    )

    compact = dispatcher.what_now({"repository"}, limit=10, compact=True)
    compact_parent = next(
        item for item in compact["blocked"] if item["task_id"] == PARENT_ID
    )
    assert compact_parent["blocking_child_task_ids"] == BLOCKING_CHILD_IDS

    with pytest.raises(NoEligibleTask, match=re.escape(BLOCKER_REASON)):
        dispatcher.claim_intent(
            "operator",
            ("repository",),
            task_id=PARENT_ID,
            approved=True,
        )

    assert {
        path: path.read_bytes() for path in (root / "registry").rglob("*.json")
    } == registry_files_before

    pickup = dispatcher.claim_next(
        "coordinated-worker",
        ("repository",),
        reconcile_first=False,
    )
    assert pickup["run"]["task_id"] == READY_CHILD_ID


def test_broad_scope_dispatcher_preserves_the_parent_claim_gate(
    registry_factory, tmp_path
):
    root = registry_factory(7, mode="write")
    _configure_parent_child_registry(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "broad-scope-state" / "bureau.sqlite3")
    dispatcher = BroadScopeDispatcher(registry, store)

    parent = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == PARENT_ID
    )
    assert parent["blocking_child_task_ids"] == BLOCKING_CHILD_IDS
    assert BLOCKER_REASON in parent["reasons"]

    with pytest.raises(NoEligibleTask, match=re.escape(BLOCKER_REASON)):
        dispatcher.claim_intent(
            "broad-scope-worker",
            ("repository",),
            task_id=PARENT_ID,
            approved=True,
        )


def test_terminal_overlays_release_parent_without_mutating_children(
    registry_factory, tmp_path
):
    root = registry_factory(7, mode="write")
    _configure_parent_child_registry(root)
    registry, store, dispatcher = _v2_dispatcher(root, tmp_path / "overlay-state")
    overlay_states = {
        PLANNED_CHILD_ID: "verified",
        BLOCKED_CHILD_ID: "cancelled",
        READY_CHILD_ID: "superseded",
    }
    with store.immediate() as connection:
        for task_id, state in overlay_states.items():
            task = registry.tasks[task_id]
            connection.execute(
                """
                INSERT INTO task_status(
                    task_id,task_sha256,plan_sha256,state,updated_at
                ) VALUES(?,?,?,?,?)
                """,
                (
                    task_id,
                    task.sha256,
                    plan_sha256(registry, task.initiative),
                    state,
                    legacy.utc_now(),
                ),
            )

    parent = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == PARENT_ID
    )
    assert parent["eligible"] is True
    assert parent["blocking_child_task_ids"] == []
    assert dispatcher.what_now({"repository"})["selected"]["task_id"] == PARENT_ID
    assert registry.tasks[PLANNED_CHILD_ID].state == "planned"
    assert registry.tasks[BLOCKED_CHILD_ID].state == "blocked"
    assert registry.tasks[READY_CHILD_ID].state == "ready"


def test_unknown_and_stale_child_states_remain_nonterminal(registry_factory):
    root = registry_factory(7)
    _configure_parent_child_registry(root)
    registry = Registry.load(root)
    projection = registry.parent_child_projection(
        PARENT_ID,
        {
            PLANNED_CHILD_ID: "unknown",
            BLOCKED_CHILD_ID: "stale",
            READY_CHILD_ID: "verified",
        },
    )

    assert projection.blocking_child_task_ids == (
        PLANNED_CHILD_ID,
        BLOCKED_CHILD_ID,
    )
    assert projection.blocker_reason == (
        "parent task blocked by nonterminal child tasks: "
        f"{PLANNED_CHILD_ID}, {BLOCKED_CHILD_ID}"
    )


def test_registry_terminal_children_ignore_nonterminal_overlays(registry_factory):
    root = registry_factory(7)
    _configure_parent_child_registry(root)
    registry = Registry.load(root)
    projection = registry.parent_child_projection(
        PARENT_ID,
        {
            SUPERSEDED_CHILD_ID: "stale",
            CANCELLED_CHILD_ID: "unknown",
        },
    )

    assert projection.nonterminal_child_task_ids == tuple(BLOCKING_CHILD_IDS)
    assert projection.blocking_child_task_ids == tuple(BLOCKING_CHILD_IDS)


def test_independently_executable_true_allows_parent_pickup(
    registry_factory, tmp_path
):
    root = registry_factory(7, mode="write")
    _configure_parent_child_registry(root)
    _set_metadata(root, PARENT_ID, "independently_executable", True)
    registry, _store, dispatcher = _v2_dispatcher(
        root,
        tmp_path / "independent-state",
    )

    projection = registry.parent_child_projection(PARENT_ID)
    assert projection.nonterminal_child_task_ids == tuple(BLOCKING_CHILD_IDS)
    assert projection.blocking_child_task_ids == ()
    pickup = dispatcher.claim_next(
        "independent-worker",
        ("repository",),
        reconcile_first=False,
    )
    assert pickup["run"]["task_id"] == PARENT_ID


def test_legacy_dispatcher_uses_the_same_parent_blocker(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(7, mode="write")
    _configure_parent_child_registry(root)
    state_root = tmp_path / "legacy-state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))
    registry = legacy.Registry.load(root)
    store = legacy.StateStore(state_root / "bureau.sqlite3")
    dispatcher = legacy.Dispatcher(registry, store)

    parent = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == PARENT_ID
    )
    assert parent["eligible"] is False
    assert parent["blocking_child_task_ids"] == BLOCKING_CHILD_IDS
    assert BLOCKER_REASON in parent["reasons"]
    pickup = dispatcher.claim_next("legacy-worker", ("repository",))
    assert pickup["run"]["task_id"] == READY_CHILD_ID


def test_real_t004_registry_contract_has_explicit_bounded_relationships():
    root = Path(__file__).parents[1]
    registry = Registry.load(root)

    t004 = registry.parent_child_projection(
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T004"
    )
    assert t004.child_task_ids == (
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T009",
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T010",
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T011",
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T012",
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T013",
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T014",
    )
    assert t004.blocking_child_task_ids == (
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T010",
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T012",
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T014",
    )

    t010 = registry.parent_child_projection(
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T010"
    )
    assert t010.parent_task_id == "REPOGROUND-LEGACY-RECONCILIATION-V1-T004"
    assert t010.blocking_child_task_ids == (
        "REPOGROUND-LEGACY-RECONCILIATION-V1-T017",
    )
