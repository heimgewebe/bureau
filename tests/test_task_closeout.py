from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import legacy, runtime_refresh, task_specs
from bureau.core import Dispatcher, Registry, StateError, StateStore, verification_stamp
from bureau.legacy import NoEligibleTask
from bureau.task_closeout import (
    BUNDLE_KIND,
    VERIFICATION_KIND,
    apply_task_no_run_closeout,
    preview_task_no_run_closeout,
)
from bureau.v2 import authoritative_task_registry, fail_run, plan_sha256, task_revision_sha256

NOW = "2026-08-24T04:00:00Z"
OBSERVED_AT = "2026-08-24T03:59:00Z"
REVIEWER = "controller:chatgpt:self-review"
OBSERVER = "controller:chatgpt:operator-observation"


def _setup(registry_factory, tmp_path: Path):
    root = registry_factory(task_count=1)
    registry = Registry.load(root)
    state_root = tmp_path / "state"
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    task = registry.tasks["BUR-TEST-001-T001"]
    with store.immediate() as connection:
        task_specs.put(
            connection,
            task.raw,
            idempotency_key="seed-no-run-closeout-task",
            expected_revision=None,
            source="test-seed",
        )
    return root, registry, store, task.id


def _current_spec(store: StateStore, task_id: str):
    with store.connect() as connection:
        return task_specs.get_current(connection, task_id)


def _bound_context(registry: Registry, store: StateStore, task_id: str):
    effective, _, _ = authoritative_task_registry(registry, store)
    task = effective.tasks[task_id]
    return task, task_revision_sha256(task.raw), plan_sha256(effective, task.initiative)


def _evidence_path(
    tmp_path: Path,
    registry: Registry,
    store: StateStore,
    task_id: str,
    *,
    observer: str = OBSERVER,
) -> Path:
    task, task_sha256, current_plan_sha256 = _bound_context(registry, store, task_id)
    criterion = task.acceptance[0]
    scope = criterion["verifier_config"]["observation_scope"]
    criterion_id = criterion["id"]
    bundle = {
        "schema_version": 1,
        "kind": BUNDLE_KIND,
        "task_id": task_id,
        "task_sha256": task_sha256,
        "plan_sha256": current_plan_sha256,
        "evidence": {
            criterion_id: {
                "schema_version": 1,
                "kind": "bureau.acceptance_evidence",
                "criterion_id": criterion_id,
                "evidence_type": "manual_observation",
                "source": {
                    "authority": "manual",
                    "reference": f"direct-work:{task_id}:{criterion_id}",
                },
                "observed_at": OBSERVED_AT,
                "revision": {
                    "task_sha256": task_sha256,
                    "plan_sha256": current_plan_sha256,
                    "observation_scope": scope,
                },
                "facts": {
                    "accepted": True,
                    "observer": observer,
                    "observation": "direct work outcome independently reviewed",
                    "observation_scope": scope,
                },
            }
        },
    }
    path = tmp_path / "no-run-evidence.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return path


def test_preview_is_read_only_and_acceptance_bound(registry_factory, tmp_path: Path) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)

    before = _current_spec(store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    after = _current_spec(store, task_id)

    assert preview["status"] == "ready-to-apply"
    assert preview["no_run_proven"] is True
    assert preview["target_state"] == "verified"
    assert preview["evaluation"]["state"] == "passed"
    assert len(preview["preview_sha256"]) == 64
    assert "bundle" not in preview
    assert "existing_verification" not in preview
    assert before == after


def test_apply_verifies_without_creating_run(
    registry_factory, tmp_path: Path
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )

    receipt = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )

    current = _current_spec(store, task_id)
    assert current is not None
    assert current["spec"]["state"] == "verified"
    verification = current["spec"]["metadata"]["verification"]
    assert verification["kind"] == VERIFICATION_KIND
    assert verification["receipt_sha256"] == receipt["receipt_sha256"]
    assert verification["receipt"]["evidence"]
    assert receipt["idempotent"] is False
    with store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM runs WHERE task_id=?", (task_id,)).fetchone()[
                0
            ]
            == 0
        )
        status = connection.execute(
            "SELECT task_sha256,plan_sha256,state,receipt_sha256 FROM task_status WHERE task_id=?",
            (task_id,),
        ).fetchone()
        assert status is not None
        assert status["task_sha256"] == receipt["task_sha256"]
        assert status["plan_sha256"] == receipt["plan_sha256"]
        assert status["state"] == "verified"
        assert status["receipt_sha256"] == receipt["receipt_sha256"]
    projection_replay = store.replay_projection()
    assert projection_replay["matches_current"] is True
    effective, _, _ = authoritative_task_registry(registry, store)
    stamp = verification_stamp(effective, store, task_id)
    assert stamp["receipt_sha256"] == receipt["receipt_sha256"]
    assert stamp["task_sha256"] == receipt["task_sha256"]
    assert stamp["plan_sha256"] == receipt["plan_sha256"]


def test_apply_blocks_claim_from_dispatcher_instantiated_before_closeout(
    registry_factory, tmp_path: Path
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    stale_dispatcher = Dispatcher(registry, store)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    apply_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"], now=NOW,
    )
    with pytest.raises(NoEligibleTask, match="state is verified"):
        stale_dispatcher.claim_next(
            "worker", ("repository",), reconcile_first=False
        )
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 0



def test_idempotent_replay_backfills_historical_task_projection(
    registry_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    real_append = store.append_task_projection_delta
    monkeypatch.setattr(
        store, "append_task_projection_delta", lambda *args, **kwargs: None
    )
    first = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert first["idempotent"] is False
    with pytest.raises(StateError, match="replayed state projection does not match"):
        store.replay_projection()

    monkeypatch.setattr(store, "append_task_projection_delta", real_append)
    second = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert second["idempotent"] is True
    assert store.replay_projection()["matches_current"] is True

    with store.connect() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    third = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert third["idempotent"] is True
    with store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            == event_count
        )


def test_idempotent_replay_refuses_unrelated_drift_while_backfilling(
    registry_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    real_append = store.append_task_projection_delta
    monkeypatch.setattr(
        store, "append_task_projection_delta", lambda *args, **kwargs: None
    )
    apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    monkeypatch.setattr(store, "append_task_projection_delta", real_append)
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO task_status("
            "task_id,state,receipt_sha256,updated_at,task_sha256,plan_sha256"
            ") VALUES(?,?,?,?,?,?)",
            ("UNRELATED-TASK", "verified", "a" * 64, NOW, "b" * 64, "c" * 64),
        )

    with pytest.raises(StateError, match="refuses unrelated StateStore drift"):
        apply_task_no_run_closeout(
            registry,
            store,
            task_id,
            evidence,
            reviewer=REVIEWER,
            expected_preview_sha256=preview["preview_sha256"],
            now=NOW,
        )


def test_idempotent_replay_ignores_unrelated_drift_when_target_is_journaled(
    registry_factory, tmp_path: Path
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    first = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert first["idempotent"] is False
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO task_status("
            "task_id,state,receipt_sha256,updated_at,task_sha256,plan_sha256"
            ") VALUES(?,?,?,?,?,?)",
            ("UNRELATED-TASK", "verified", "a" * 64, NOW, "b" * 64, "c" * 64),
        )
    with store.connect() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    second = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert second["idempotent"] is True
    with store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            == event_count
        )




def test_idempotent_replay_skips_unrelated_root_mismatch_when_target_is_journaled(
    registry_factory, tmp_path: Path
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    first = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert first["idempotent"] is False

    store.set_initiative_state("INIT-A", "active")
    with store.immediate() as connection:
        connection.execute(
            "UPDATE initiative_status SET state='waiting' WHERE initiative_id='INIT-A'"
        )
    store.set_initiative_state("INIT-B", "active")
    with pytest.raises(StateError, match="projection replay root digest mismatch"):
        store.replay_projection()
    with store.connect() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        repair_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='state-projection-repair-v1'"
        ).fetchone()[0]

    second = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert second["idempotent"] is True
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='state-projection-repair-v1'"
        ).fetchone()[0] == repair_count
    with pytest.raises(StateError, match="projection replay root digest mismatch"):
        store.replay_projection()


def test_idempotent_replay_backfills_multiple_historical_tasks_atomically(
    registry_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = registry_factory(task_count=2)
    registry = Registry.load(root)
    state_root = tmp_path / "multi-state"
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    task_ids = sorted(registry.tasks)
    with store.immediate() as connection:
        for task_id in task_ids:
            task_specs.put(
                connection,
                registry.tasks[task_id].raw,
                idempotency_key=f"seed-no-run-closeout-{task_id}",
                expected_revision=None,
                source="test-seed",
            )
    real_append = store.append_task_projection_delta
    monkeypatch.setattr(
        store, "append_task_projection_delta", lambda *args, **kwargs: None
    )
    for task_id in task_ids:
        evidence = _evidence_path(tmp_path, registry, store, task_id)
        preview = preview_task_no_run_closeout(
            registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
        )
        receipt = apply_task_no_run_closeout(
            registry,
            store,
            task_id,
            evidence,
            reviewer=REVIEWER,
            expected_preview_sha256=preview["preview_sha256"],
            now=NOW,
        )
        assert receipt["idempotent"] is False
    monkeypatch.setattr(store, "append_task_projection_delta", real_append)
    with pytest.raises(StateError, match="replayed state projection does not match"):
        store.replay_projection()

    target = task_ids[0]
    evidence = _evidence_path(tmp_path, registry, store, target)
    preview = preview_task_no_run_closeout(
        registry, store, target, evidence, reviewer=REVIEWER, now=NOW
    )
    with store.connect() as connection:
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    receipt = apply_task_no_run_closeout(
        registry,
        store,
        target,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert receipt["idempotent"] is True
    assert store.replay_projection()["matches_current"] is True
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before + 1

    other = task_ids[1]
    evidence = _evidence_path(tmp_path, registry, store, other)
    preview = preview_task_no_run_closeout(
        registry, store, other, evidence, reviewer=REVIEWER, now=NOW
    )
    with store.connect() as connection:
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    receipt = apply_task_no_run_closeout(
        registry,
        store,
        other,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert receipt["idempotent"] is True
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_idempotent_replay_repairs_later_root_mismatch(
    registry_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    real_append = store.append_task_projection_delta
    monkeypatch.setattr(
        store, "append_task_projection_delta", lambda *args, **kwargs: None
    )
    apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    monkeypatch.setattr(store, "append_task_projection_delta", real_append)
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO initiative_status(initiative_id,state,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(initiative_id) DO UPDATE SET "
            "state=excluded.state,updated_at=excluded.updated_at",
            ("BUR-TEST-001", "active", NOW),
        )
        store.event(
            connection,
            "initiative-state-set",
            {"initiative_id": "BUR-TEST-001", "state": "active"},
            initiative_id="BUR-TEST-001",
        )
    with pytest.raises(StateError, match="projection replay root digest mismatch"):
        store.replay_projection()

    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    receipt = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert receipt["idempotent"] is True
    replay = store.replay_projection()
    assert replay["matches_current"] is True
    assert replay["repair_checkpoint_count"] == 1


def test_apply_is_idempotent_for_same_verified_receipt(registry_factory, tmp_path: Path) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    first = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    revision = _current_spec(store, task_id)["revision"]

    second = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )

    assert second["idempotent"] is True
    assert second["receipt_sha256"] == first["receipt_sha256"]
    assert _current_spec(store, task_id)["revision"] == revision


def test_preview_rejects_symlink_evidence(registry_factory, tmp_path: Path) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    linked = tmp_path / "linked-evidence.json"
    linked.symlink_to(evidence)

    with pytest.raises(StateError, match="regular non-symlink"):
        preview_task_no_run_closeout(registry, store, task_id, linked, reviewer=REVIEWER, now=NOW)


def test_preview_rejects_any_existing_bureau_run(registry_factory, tmp_path: Path) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    Dispatcher(registry, store).claim_next("worker", ("repository",))
    evidence = _evidence_path(tmp_path, registry, store, task_id)

    with pytest.raises(StateError, match="Bureau run"):
        preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW)


def _failed_run_rows(store, task_id):
    with store.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM runs WHERE task_id=? ORDER BY run_id", (task_id,)
            )
        ]


def _bind_failed_history(evidence, store, task_id):
    bundle = json.loads(evidence.read_text())
    bundle["allow_terminal_failed_runs"] = True
    for item in bundle["evidence"].values():
        item["observed_at"] = legacy.utc_now()
    evidence.write_text(json.dumps(bundle))
    return [
        {
            "run_id": row["run_id"],
            "run_sha256": legacy.sha256_json(row),
            "run_hash_schema_version": 1,
        }
        for row in _failed_run_rows(store, task_id)
    ]


@pytest.mark.parametrize("repair_projection", [False, True])
def test_failed_run_history_explicit_closeout(
    registry_factory, tmp_path, monkeypatch, repair_projection
):
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    dispatcher = Dispatcher(registry, store)
    run_id = dispatcher.claim_next("worker", ("repository",))["run"]["run_id"]
    fail_run(store, run_id, "historical execution failed")
    if repair_projection:
        second = dispatcher.claim_next("worker", ("repository",))["run"]["run_id"]
        fail_run(store, second, "another historical execution failed")
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    with pytest.raises(StateError, match="Bureau run"):
        preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)
    history = _bind_failed_history(evidence, store, task_id)
    before = _failed_run_rows(store, task_id)
    preview = preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)
    assert preview["no_run_proven"] is False
    assert preview["terminal_failed_runs"] == history
    append_projection = store.append_task_projection_delta
    if repair_projection:
        monkeypatch.setattr(store, "append_task_projection_delta", lambda *_a, **_kw: None)
    result = apply_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
    )
    assert result["no_run_proven"] is False
    assert result["terminal_failed_runs"] == history
    assert _current_spec(store, task_id)["spec"]["state"] == "verified"
    assert _failed_run_rows(store, task_id) == before
    assert store.run(run_id)["reservations"] == []
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    if repair_projection:
        with pytest.raises(StateError, match="replayed state projection does not match"):
            store.replay_projection()
    else:
        assert store.replay_projection()["matches_current"] is True
    monkeypatch.setattr(store, "append_task_projection_delta", append_projection)
    again = apply_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
    )
    assert again["idempotent"] is True
    assert again["receipt_sha256"] == result["receipt_sha256"]
    assert store.replay_projection()["matches_current"] is True
    assert _failed_run_rows(store, task_id) == before


@pytest.mark.parametrize("state", ["claimed", "cancelled", "orphaned", "succeeded"])
def test_failed_run_history_rejects_other_states(registry_factory, tmp_path, state):
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    run_id = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]["run_id"]
    if state != "claimed":
        # Deliberately model corrupt/unsupported history in this isolated test DB.
        with store.immediate() as connection:
            connection.execute("UPDATE runs SET state=? WHERE run_id=?", (state, run_id))
            connection.execute("DELETE FROM reservations WHERE run_id=?", (run_id,))
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    _bind_failed_history(evidence, store, task_id)
    with pytest.raises(StateError, match="terminal failed"):
        preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)


@pytest.mark.parametrize(
    "condition", ["reservation", "external", "receipt", "envelope", "opt-in", "unbound-lease"]
)
def test_failed_run_history_rejects_ambiguous_evidence(registry_factory, tmp_path, condition):
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    run_id = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]["run_id"]
    fail_run(store, run_id, "historical failure")
    with store.immediate() as connection:
        if condition == "reservation":
            connection.execute(
                "INSERT INTO reservations VALUES(?,?,?,?,?)",
                (run_id, "repo.test", "write", 1, legacy.utc_now()),
            )
        elif condition == "external":
            connection.execute("UPDATE runs SET external_id=? WHERE run_id=?", ("unknown", run_id))
        elif condition == "receipt":
            connection.execute(
                "INSERT INTO receipts VALUES(?,?,?,?)", (run_id, "{}", "0" * 64, legacy.utc_now())
            )
        elif condition == "envelope":
            connection.execute(
                "UPDATE runs SET envelope_sha256=? WHERE run_id=?", ("0" * 64, run_id)
            )
        elif condition == "unbound-lease":
            envelope = json.loads(connection.execute(
                "SELECT envelope_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            assert envelope.get("claim_intent") is None
            envelope["lease_binding"] = {"owner_id": "unbound"}
            connection.execute(
                "UPDATE runs SET envelope_json=?, envelope_sha256=? WHERE run_id=?",
                (json.dumps(envelope), legacy.sha256_json(envelope), run_id),
            )
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    _bind_failed_history(evidence, store, task_id)
    if condition == "opt-in":
        bundle = json.loads(evidence.read_text())
        bundle["allow_terminal_failed_runs"] = "true"
        evidence.write_text(json.dumps(bundle))
    with pytest.raises(StateError, match="terminal failed"):
        preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)
    assert _current_spec(store, task_id)["spec"]["state"] == "ready"


def test_failed_run_history_blocks_concurrent_pickup(registry_factory, tmp_path):
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    dispatcher = Dispatcher(registry, store)
    first = dispatcher.claim_next("worker", ("repository",))["run"]["run_id"]
    fail_run(store, first, "historical failure")
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    _bind_failed_history(evidence, store, task_id)
    preview = preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)
    second = dispatcher.claim_next("worker", ("repository",))["run"]["run_id"]
    assert second != first
    with pytest.raises(StateError, match="terminal failed"):
        apply_task_no_run_closeout(
            registry, store, task_id, evidence, reviewer=REVIEWER,
            expected_preview_sha256=preview["preview_sha256"],
        )
    assert _current_spec(store, task_id)["spec"]["state"] == "ready"


def test_failed_run_history_rejects_changed_failed_row_at_apply(registry_factory, tmp_path):
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    run_id = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]["run_id"]
    fail_run(store, run_id, "historical failure")
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    _bind_failed_history(evidence, store, task_id)
    preview = preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)
    with store.immediate() as connection:
        connection.execute("UPDATE runs SET error=? WHERE run_id=?", ("changed error", run_id))
    with pytest.raises(StateError, match="preview changed before apply"):
        apply_task_no_run_closeout(
            registry, store, task_id, evidence, reviewer=REVIEWER,
            expected_preview_sha256=preview["preview_sha256"],
        )
    assert _current_spec(store, task_id)["spec"]["state"] == "ready"


@pytest.mark.parametrize("repair_projection", [False, True])
def test_failed_run_history_rejects_live_pickup_lease(
    registry_factory, tmp_path, monkeypatch, repair_projection
):
    from test_v2 import coordinated_lease_database, prepare_coordinated_registry, setup

    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    with store.immediate() as connection:
        task_specs.put(
            connection, registry.tasks[task_id].raw,
            idempotency_key="seed-failed-history-coordinated",
            expected_revision=None, source="test-seed",
        )
    dispatcher = Dispatcher(registry, store)
    intent = dispatcher.claim_intent(
        "operator", ("repository",), task_id=task_id,
        base_dir=tmp_path / "worktrees", approved=True,
    )["intent"]
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    fail_run(store, intent["run_id"], "historical failed execution")
    from bureau.task_closeout import _terminal_failed_history

    # A valid intent/envelope must also bind the current historical run row.
    # Restore the fixture within the same transaction so replay history is unchanged.
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET plan_sha256=? WHERE run_id=?", ("0" * 64, intent["run_id"])
        )
        with pytest.raises(StateError, match="claim binding mismatch"):
            _terminal_failed_history(connection, task_id, allow_failed=True)
        connection.execute(
            "UPDATE runs SET plan_sha256=? WHERE run_id=?",
            (intent["plan_sha256"], intent["run_id"]),
        )
    # The committed lease binding selects the authoritative DB, not a process default.
    _, wrong_default = coordinated_lease_database(
        tmp_path / "other-store", intent, omit=set(intent["required_resource_keys"])
    )
    monkeypatch.setattr(runtime_refresh, "DEFAULT_GRABOWSKI_RESOURCE_DB", wrong_default)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    _bind_failed_history(evidence, store, task_id)
    with pytest.raises(StateError, match="live or ambiguous pickup leases"):
        preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM leases")
    preview = preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)
    unavailable = database.with_suffix(".unavailable")
    database.rename(unavailable)
    try:
        with pytest.raises(StateError, match="live or ambiguous pickup leases"):
            apply_task_no_run_closeout(
                registry, store, task_id, evidence, reviewer=REVIEWER,
                expected_preview_sha256=preview["preview_sha256"],
            )
    finally:
        unavailable.rename(database)
    before = _failed_run_rows(store, task_id)
    append_projection = store.append_task_projection_delta
    if repair_projection:
        monkeypatch.setattr(store, "append_task_projection_delta", lambda *_a, **_kw: None)
    result = apply_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
    )
    assert result["status"] == "verified"
    assert _failed_run_rows(store, task_id) == before
    if repair_projection:
        with pytest.raises(StateError, match="replayed state projection does not match"):
            store.replay_projection()
    else:
        assert store.replay_projection()["matches_current"] is True
    monkeypatch.setattr(store, "append_task_projection_delta", append_projection)
    # A committed receipt must remain replayable after the historical external
    # lease database is lost. No new closeout may be authorized in that state.
    database.rename(unavailable)
    try:
        again = apply_task_no_run_closeout(
            registry, store, task_id, evidence, reviewer=REVIEWER,
            expected_preview_sha256=preview["preview_sha256"],
        )
        assert again["idempotent"] is True
        assert again["receipt_sha256"] == result["receipt_sha256"]
        assert store.replay_projection()["matches_current"] is True
        assert _failed_run_rows(store, task_id) == before
    finally:
        unavailable.rename(database)


def test_failed_run_history_schema_extension_preserves_receipt(registry_factory, tmp_path):
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    run_id = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]["run_id"]
    fail_run(store, run_id, "historical failure")
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    _bind_failed_history(evidence, store, task_id)
    before = _failed_run_rows(store, task_id)
    preview = preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)
    first = apply_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
    )
    with store.immediate() as connection:
        connection.execute("ALTER TABLE runs ADD COLUMN later_optional TEXT")
    second = apply_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
    )
    assert second["idempotent"] is True
    assert second["receipt_sha256"] == first["receipt_sha256"]
    assert store.replay_projection()["matches_current"] is True
    assert _failed_run_rows(store, task_id) == [
        dict(row, later_optional=None) for row in before
    ]


def test_failed_run_history_requires_post_failure_acceptance(registry_factory, tmp_path):
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    run_id = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]["run_id"]
    fail_run(store, run_id, "historical failure")
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    _bind_failed_history(evidence, store, task_id)
    bundle = json.loads(evidence.read_text())
    from datetime import datetime, timedelta, timezone
    before_failure = datetime.fromisoformat(
        _failed_run_rows(store, task_id)[0]["updated_at"]
    ) - timedelta(seconds=1)
    for item in bundle["evidence"].values():
        item["observed_at"] = before_failure.astimezone(timezone.utc).isoformat()
    evidence.write_text(json.dumps(bundle))
    with pytest.raises(StateError, match="observed after the last failure"):
        preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER)


def test_preview_rejects_reviewer_equal_to_evidence_producer(
    registry_factory, tmp_path: Path
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id, observer=REVIEWER)

    with pytest.raises(StateError, match="reviewer must differ"):
        preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW)


def test_preview_rejects_whitespace_padded_reviewer_identity(
    registry_factory, tmp_path: Path
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(
        tmp_path, registry, store, task_id, observer=f"  {REVIEWER}  "
    )
    with pytest.raises(StateError, match="reviewer must differ"):
        preview_task_no_run_closeout(
            registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
        )


def test_preview_rejects_non_manual_acceptance(registry_factory, tmp_path: Path) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    current = _current_spec(store, task_id)
    assert current is not None
    mutated = json.loads(json.dumps(current["spec"]))
    mutated["acceptance"] = [
        {
            "id": "proof",
            "assertion": "PR merged",
            "evidence_type": "object",
            "verifier": "code_merged",
            "verifier_config": {
                "repository": "heimgewebe/example",
                "pull_request": 1,
                "head_sha": "1" * 40,
                "base_ref": "main",
            },
        }
    ]
    with store.immediate() as connection:
        task_specs.put(
            connection,
            mutated,
            idempotency_key="change-to-non-manual",
            expected_revision=current["revision"],
            source="test",
        )
    _task, task_sha256, current_plan_sha256 = _bound_context(registry, store, task_id)
    bundle = {
        "schema_version": 1,
        "kind": BUNDLE_KIND,
        "task_id": task_id,
        "task_sha256": task_sha256,
        "plan_sha256": current_plan_sha256,
        "evidence": {"proof": {}},
    }
    evidence = tmp_path / "non-manual-evidence.json"
    evidence.write_text(json.dumps(bundle), encoding="utf-8")

    with pytest.raises(StateError, match="supports only manual_observation"):
        preview_task_no_run_closeout(registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW)


def test_apply_rejects_changed_preview(registry_factory, tmp_path: Path) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    current = _current_spec(store, task_id)
    assert current is not None
    mutated = json.loads(json.dumps(current["spec"]))
    mutated["title"] = "Changed after preview"
    with store.immediate() as connection:
        task_specs.put(
            connection,
            mutated,
            idempotency_key="preview-drift",
            expected_revision=current["revision"],
            source="test",
        )

    with pytest.raises(StateError, match=r"binding mismatch|preview changed"):
        apply_task_no_run_closeout(
            registry,
            store,
            task_id,
            evidence,
            reviewer=REVIEWER,
            expected_preview_sha256=preview["preview_sha256"],
            now=NOW,
        )


def test_cli_preview_is_read_only_and_apply_is_coordination_mutation() -> None:
    preview = bureau_cli.parser().parse_args(
        [
            "task-no-run-closeout",
            "BUR-TEST-001-T001",
            "--evidence",
            "/tmp/evidence.json",
            "--reviewer",
            REVIEWER,
        ]
    )
    apply = bureau_cli.parser().parse_args(
        [
            "task-no-run-closeout",
            "BUR-TEST-001-T001",
            "--evidence",
            "/tmp/evidence.json",
            "--reviewer",
            REVIEWER,
            "--apply",
            "--expected-preview-sha256",
            "a" * 64,
        ]
    )

    assert bureau_cli._command_mutates(preview) is False
    assert bureau_cli._command_effect_scope(preview) == "read_only"
    assert bureau_cli._command_mutates(apply) is True
    assert bureau_cli._command_effect_scope(apply) == "coordination_state_mutation"
