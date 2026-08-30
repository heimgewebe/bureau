from __future__ import annotations

import json
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import task_specs
from bureau.core import Dispatcher, Registry, StateError, StateStore, verification_stamp
from bureau.legacy import NoEligibleTask
from bureau.task_closeout import (
    BUNDLE_KIND,
    VERIFICATION_KIND,
    apply_task_no_run_closeout,
    preview_task_no_run_closeout,
)
from bureau.v2 import authoritative_task_registry, plan_sha256, task_revision_sha256

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
