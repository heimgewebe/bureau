from __future__ import annotations

import json

from bureau.core import Dispatcher, Registry, StateStore
from bureau.frontier import FrontierPolicy, build_frontier_projection


def _queue_path(root):
    return root / "registry/queue.json"


def _task_path(root, task_id):
    return root / "registry/tasks" / f"{task_id}.json"


def _remove_from_queue(root, task_id: str) -> None:
    path = _queue_path(root)
    queue = json.loads(path.read_text())
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    path.write_text(json.dumps(queue))


def _set_priority(root, task_id: str, lane: str) -> None:
    path = _task_path(root, task_id)
    task = json.loads(path.read_text())
    task["priority"]["lane"] = lane
    path.write_text(json.dumps(task))


def _lane_ids(projection, lane: str) -> list[str]:
    return [item["task_id"] for item in projection["lanes"][lane]]


def _card(projection, task_id: str):
    for lane in projection["lanes"].values():
        for item in lane:
            if item["task_id"] == task_id:
                return item
    raise AssertionError(f"missing projected task {task_id}")


def test_ready_task_needs_no_manual_queue_admission(registry_factory, tmp_path):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    _remove_from_queue(root, task_id)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    projection = build_frontier_projection(
        Registry.load(root), store, check_runtime=False
    )

    assert projection["queue_authoritative"] is False
    assert projection["queue_role"] == "compatibility_projection_only"
    assert task_id in _lane_ids(projection, "now")
    card = _card(projection, task_id)
    assert card["structurally_eligible"] is True
    assert "task is not queued in registry/queue.json" not in card["structural_reasons"]
    assert projection["compatibility_queue"]["lanes"]["now"] == [task_id]


def test_state_store_taskspec_wins_over_git_projection(registry_factory, tmp_path):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    imported = store.import_registry_task_specs(registry)
    assert imported["imported"] == 1
    current = store.task_spec(task_id)
    assert current is not None
    spec = dict(current["spec"])
    spec["state"] = "blocked"
    spec["priority"] = {"lane": "next", "rank": 7}
    changed = store.put_task_spec(
        spec,
        idempotency_key="frontier-state-store-precedence",
        expected_revision=1,
        source="test",
    )
    assert changed["revision"] == 2

    projection = build_frontier_projection(registry, store, check_runtime=False)
    card = _card(projection, task_id)

    assert projection["authority"]["kind"] == "bureau-state-store-task-specs"
    assert card["task_spec_revision"] == 2
    assert card["declared_state"] == "blocked"
    assert card["priority"] == {"lane": "next", "rank": 7}
    assert card["projected_lane"] == "blocked"
    # Git remains unchanged and therefore cannot silently override StateStore truth.
    assert json.loads(_task_path(root, task_id).read_text())["state"] == "ready"


def test_dispatcher_uses_state_store_taskspec_priority_for_claim(registry_factory, tmp_path):
    root = registry_factory(2)
    first = "BUR-TEST-001-T001"
    second = "BUR-TEST-001-T002"
    registry = Registry.load(root)
    assert registry.ordered_tasks()[0].id == first

    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    store.import_registry_task_specs(registry)
    first_current = store.task_spec(first)
    second_current = store.task_spec(second)
    assert first_current is not None
    assert second_current is not None

    first_spec = dict(first_current["spec"])
    first_spec["priority"] = {"lane": "now", "rank": 50}
    first_changed = store.put_task_spec(
        first_spec,
        idempotency_key="dispatcher-state-store-priority-first",
        expected_revision=1,
        source="test",
    )
    assert first_changed["revision"] == 2

    second_spec = dict(second_current["spec"])
    second_spec["priority"] = {"lane": "now", "rank": 0}
    changed = store.put_task_spec(
        second_spec,
        idempotency_key="dispatcher-state-store-priority-second",
        expected_revision=1,
        source="test",
    )
    assert changed["revision"] == 2

    dispatcher = Dispatcher(registry, store)
    assert dispatcher.task_authority["kind"] == "bureau-state-store-task-specs"
    assert dispatcher.registry.tasks[second].rank == 0
    assert dispatcher.source_registry.tasks[second].rank != 0

    claimed = dispatcher.claim_next("worker", ("repository",))
    assert claimed["run"]["task_id"] == second
    assert claimed["run"]["task_sha256"] == dispatcher.registry.tasks[second].sha256
    assert claimed["run"]["task_sha256"] != changed["spec_sha256"]
    assert dispatcher.task_revisions[second]["spec_sha256"] == changed["spec_sha256"]


def test_terminal_state_never_enters_executable_projection(registry_factory, tmp_path):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    store.import_registry_task_specs(registry)
    current = store.task_spec(task_id)
    assert current is not None
    spec = dict(current["spec"])
    spec["state"] = "cancelled"
    store.put_task_spec(
        spec,
        idempotency_key="frontier-terminal-exclusion",
        expected_revision=1,
        source="test",
    )

    projection = build_frontier_projection(registry, store, check_runtime=False)

    executable = {
        task
        for lane in ("now", "next", "later")
        for task in _lane_ids(projection, lane)
    }
    assert task_id not in executable
    assert projection["summary"]["terminal_excluded_count"] == 1
    assert all(
        task_id not in projection["compatibility_queue"]["lanes"][lane]
        for lane in ("now", "next", "later")
    )


def test_actor_and_structural_blockers_are_separate(registry_factory, tmp_path):
    root = registry_factory(2)
    first = "BUR-TEST-001-T001"
    second = "BUR-TEST-001-T002"
    first_path = _task_path(root, first)
    task = json.loads(first_path.read_text())
    task["depends_on"] = [second]
    task["execution"]["policy"] = "review-before-effect"
    first_path.write_text(json.dumps(task))
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    projection = build_frontier_projection(
        Registry.load(root),
        store,
        capabilities={"repository"},
        check_runtime=False,
    )
    card = _card(projection, first)

    assert any(reason.startswith("dependency ") for reason in card["structural_reasons"])
    assert any(reason.startswith("execution is ") for reason in card["actor_dependent_reasons"])
    assert all(
        not reason.startswith("execution is ") for reason in card["structural_reasons"]
    )
    assert card["projected_lane"] == "blocked"


def test_actor_eligibility_summary_is_independent_of_structural_gates(
    registry_factory, tmp_path
):
    root = registry_factory(2)
    first = "BUR-TEST-001-T001"
    second = "BUR-TEST-001-T002"
    first_path = _task_path(root, first)
    task = json.loads(first_path.read_text())
    task["depends_on"] = [second]
    first_path.write_text(json.dumps(task))
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    projection = build_frontier_projection(
        Registry.load(root), store, check_runtime=False
    )
    card = _card(projection, first)

    assert card["actor_eligible"] is True
    assert card["claim_eligible"] is False
    assert projection["summary"]["actor_eligible_count"] == 2
    assert projection["summary"]["claim_eligible_count"] == 1


def test_projection_is_deterministic_for_same_inputs(registry_factory, tmp_path):
    root = registry_factory(4)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    registry = Registry.load(root)

    first = build_frontier_projection(registry, store, check_runtime=False)
    second = build_frontier_projection(registry, store, check_runtime=False)

    assert first["projection_sha256"] == second["projection_sha256"]
    assert {
        lane: _lane_ids(first, lane) for lane in first["lanes"]
    } == {
        lane: _lane_ids(second, lane) for lane in second["lanes"]
    }
    assert first["work_balls"] == second["work_balls"]


def test_now_supply_prefers_disjoint_resource_domains(registry_factory, tmp_path):
    root = registry_factory(3, mode="write")
    ids = [f"BUR-TEST-001-T{index:03d}" for index in range(1, 4)]
    queue_path = _queue_path(root)
    queue = json.loads(queue_path.read_text())
    queue["lanes"] = {"now": [], "next": ids, "later": []}
    queue_path.write_text(json.dumps(queue))
    for task_id in ids:
        _set_priority(root, task_id, "next")
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    projection = build_frontier_projection(
        Registry.load(root),
        store,
        policy=FrontierPolicy(
            now_floor=2,
            now_target=2,
            max_now_promotions=2,
            work_ball_limit=3,
        ),
        check_runtime=False,
    )

    assert _lane_ids(projection, "now") == ids[:2]
    assert [item["task_id"] for item in projection["work_balls"][:2]] == ids[:2]
    assert projection["summary"]["now_refill_promotion_count"] == 2


def test_active_run_projects_to_in_flight(registry_factory, tmp_path):
    root = registry_factory(1)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    run = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]

    projection = build_frontier_projection(registry, store, check_runtime=False)
    card = _card(projection, run["task_id"])

    assert card["projected_lane"] == "in_flight"
    assert card["active_runs"][0]["run_id"] == run["run_id"]
    assert run["task_id"] not in _lane_ids(projection, "now")


def test_verifying_run_projects_to_closeout(registry_factory, tmp_path):
    root = registry_factory(1)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    run = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET state='verifying' WHERE run_id=?", (run["run_id"],)
        )

    projection = build_frontier_projection(registry, store, check_runtime=False)
    card = _card(projection, run["task_id"])

    assert card["projected_lane"] == "closeout"
    assert run["task_id"] not in _lane_ids(projection, "next")
