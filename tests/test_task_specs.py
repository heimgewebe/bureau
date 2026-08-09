from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bureau import task_specs
from bureau.legacy import StateError
from bureau.v2 import StateStore


def _spec(task_id: str = "TEST-T001", *, title: str = "first", marker: str = "a") -> dict:
    return {
        "id": task_id,
        "initiative": "TEST",
        "title": title,
        "state": "planned",
        "priority": {"lane": "later", "rank": 1},
        "depends_on": [],
        "mode": "manual",
        "policy": "review-before-effect",
        "claims": [],
        "required_capabilities": [],
        "acceptance": [],
        "metadata": {"marker": marker, "nested": {"keep": [1, 2, 3]}},
    }


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)


def test_schema_upgrade_exposes_task_spec_tables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    integrity = store.integrity()
    assert integrity["schema_version"] == 5
    with store.connect() as connection:
        task_specs.validate_schema(connection)


def test_revision_cas_and_idempotent_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.put_task_spec(
        _spec(), idempotency_key="create-1", expected_revision=None, source="test"
    )
    assert first["revision"] == 1
    assert first["changed"] is True

    replay = store.put_task_spec(
        _spec(), idempotency_key="create-1", expected_revision=None, source="test"
    )
    assert replay["revision"] == 1
    assert replay["idempotent_replay"] is True

    second_spec = _spec(title="second", marker="b")
    second = store.put_task_spec(
        second_spec, idempotency_key="update-2", expected_revision=1, source="test"
    )
    assert second["revision"] == 2
    assert second["parent_revision"] == 1
    assert store.task_spec("TEST-T001")["spec"] == second_spec

    with pytest.raises(StateError, match="stale TaskSpec revision baseline"):
        store.put_task_spec(
            _spec(title="stale"),
            idempotency_key="stale-3",
            expected_revision=1,
            source="test",
        )


def test_reverting_to_prior_content_creates_a_new_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = _spec()
    store.put_task_spec(original, idempotency_key="rev-1", expected_revision=None, source="test")
    store.put_task_spec(
        _spec(title="changed"), idempotency_key="rev-2", expected_revision=1, source="test"
    )
    reverted = store.put_task_spec(
        original, idempotency_key="rev-3", expected_revision=2, source="test"
    )
    assert reverted["revision"] == 3
    assert reverted["spec_sha256"] == task_specs.task_spec_digest(original)


def test_idempotency_key_conflict_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_task_spec(_spec(), idempotency_key="same", expected_revision=None, source="test")
    with pytest.raises(StateError, match="idempotency key"):
        store.put_task_spec(
            _spec(title="different"),
            idempotency_key="same",
            expected_revision=1,
            source="test",
        )


def test_identical_semantics_do_not_create_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_task_spec(_spec(), idempotency_key="a", expected_revision=None, source="test")
    result = store.put_task_spec(_spec(), idempotency_key="b", expected_revision=1, source="test")
    assert result["changed"] is False
    assert result["revision"] == 1
    with store.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM task_spec_revisions").fetchone()[0]
    assert count == 1


def test_concurrent_changes_serialize_through_cas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_task_spec(_spec(), idempotency_key="seed", expected_revision=None, source="test")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(name: str) -> None:
        barrier.wait()
        try:
            store.put_task_spec(
                _spec(title=name, marker=name),
                idempotency_key=f"worker-{name}",
                expected_revision=1,
                source="concurrency-test",
            )
            outcomes.append("ok")
        except StateError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert sum("stale TaskSpec revision baseline" in value for value in outcomes) == 1
    assert store.task_spec("TEST-T001")["revision"] == 2


def test_crash_rolls_back_revision_projection_and_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RuntimeError, match="crash"), store.immediate() as connection:
        task_specs.put(
            connection,
            _spec(),
            idempotency_key="rollback",
            expected_revision=None,
            source="test",
        )
        raise RuntimeError("crash")
    assert store.task_spec("TEST-T001") is None
    with store.connect() as connection:
        events = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=?", (task_specs.TASK_SPEC_EVENT_TYPE,)
        ).fetchone()[0]
    assert events == 0


def test_replay_after_multiple_revisions_matches_projection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_task_spec(_spec(), idempotency_key="r1", expected_revision=None, source="test")
    store.put_task_spec(
        _spec(title="two"), idempotency_key="r2", expected_revision=1, source="test"
    )
    store.put_task_spec(
        _spec(title="three"), idempotency_key="r3", expected_revision=2, source="test"
    )
    replay = store.replay_projection()
    assert replay["matches_current"] is True
    assert replay["task_specs"]["matches_current"] is True
    assert replay["task_specs"]["event_count"] == 3
    assert replay["authoritative_root_sha256"] == replay["current_authoritative_root_sha256"]


def test_unknown_task_spec_event_version_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_task_spec(_spec(), idempotency_key="v1", expected_revision=None, source="test")
    with store.connect() as connection:
        connection.execute(
            "UPDATE events SET event_schema_version=99 WHERE event_type=?",
            (task_specs.TASK_SPEC_EVENT_TYPE,),
        )
    with pytest.raises(StateError, match="unsupported TaskSpec event schema version"):
        store.replay_projection()


def test_unknown_event_type_still_fails_t002_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO events(run_id,event_type,event_schema_version,payload_json,created_at) "
            "VALUES(NULL,'unknown-t003-event',1,'{}','now')"
        )
    with pytest.raises(StateError, match="unknown Bureau state event type"):
        store.replay_projection()


def test_schema_drift_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connect() as connection:
        connection.execute("DROP TABLE task_spec_mutations")
    with pytest.raises(StateError, match="TaskSpec schema drift"):
        store.replay_projection()


def test_digest_tampering_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_task_spec(_spec(), idempotency_key="tamper", expected_revision=None, source="test")
    with store.connect() as connection:
        connection.execute(
            "UPDATE task_spec_revisions SET spec_json=? WHERE task_id='TEST-T001' AND revision=1",
            (json.dumps(_spec(title="tampered")),),
        )
    with pytest.raises(StateError, match="digest mismatch"):
        store.task_spec("TEST-T001")


def test_legacy_import_is_lossless_idempotent_and_detects_divergence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    raw = _spec("LEGACY-T001", title="legacy", marker="preserve")
    registry = SimpleNamespace(tasks={"LEGACY-T001": SimpleNamespace(raw=raw)})
    first = store.import_registry_task_specs(registry)
    assert first["imported"] == 1
    assert first["unchanged"] == 0
    assert store.task_spec("LEGACY-T001")["spec"] == raw

    second = store.import_registry_task_specs(registry)
    assert second["imported"] == 0
    assert second["unchanged"] == 1

    changed = _spec("LEGACY-T001", title="git changed")
    divergent = SimpleNamespace(tasks={"LEGACY-T001": SimpleNamespace(raw=changed)})
    with pytest.raises(StateError, match="legacy TaskSpec divergence"):
        store.import_registry_task_specs(divergent)


def test_registration_seeds_missing_legacy_specs_and_preserves_state_store_divergence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state_owned = _spec("LEGACY-B")
    state_owned["state"] = "ready"
    store.put_task_spec(
        state_owned,
        idempotency_key="state-owned",
        expected_revision=None,
        source="lifecycle-reconcile",
    )
    registry = SimpleNamespace(
        tasks={
            "LEGACY-A": SimpleNamespace(raw=_spec("LEGACY-A")),
            "LEGACY-B": SimpleNamespace(raw=_spec("LEGACY-B")),
        }
    )

    result = store.register_task_spec_with_legacy_import(
        registry,
        _spec("NEW-TASK"),
        idempotency_key="reviewed-new-task",
        expected_revision=None,
        source="reviewed",
    )

    seeded = result["legacy_task_spec_import"]
    assert seeded["mode"] == "seed-missing-preserve-state-store"
    assert seeded["imported"] == 1
    assert seeded["unchanged"] == 0
    assert seeded["divergent_preserved_count"] == 1
    assert seeded["divergent_preserved_sample"] == ["LEGACY-B"]
    assert seeded["divergent_preserved_truncated"] is False
    assert store.task_spec("LEGACY-A")["spec"]["state"] == "planned"
    assert store.task_spec("LEGACY-B")["spec"] == state_owned
    assert store.task_spec("NEW-TASK")["spec"]["state"] == "planned"

    with pytest.raises(StateError, match="legacy TaskSpec divergence"):
        store.import_registry_task_specs(registry)


def test_registration_and_legacy_import_share_one_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_task_spec(
        _spec("BOUND-TASK"),
        idempotency_key="conflicting-key",
        expected_revision=None,
        source="seed",
    )
    registry = SimpleNamespace(tasks={"LEGACY-A": SimpleNamespace(raw=_spec("LEGACY-A"))})
    with pytest.raises(StateError, match="idempotency key"):
        store.register_task_spec_with_legacy_import(
            registry,
            _spec("NEW-TASK"),
            idempotency_key="conflicting-key",
            expected_revision=None,
            source="reviewed",
        )
    assert store.task_spec("LEGACY-A") is None
    assert store.task_spec("NEW-TASK") is None


def test_failed_import_rolls_back_all_tasks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    existing = _spec("B-TASK", title="state")
    store.put_task_spec(existing, idempotency_key="existing", expected_revision=None, source="test")
    registry = SimpleNamespace(
        tasks={
            "A-TASK": SimpleNamespace(raw=_spec("A-TASK")),
            "B-TASK": SimpleNamespace(raw=_spec("B-TASK", title="divergent")),
        }
    )
    with pytest.raises(StateError, match="legacy TaskSpec divergence"):
        store.import_registry_task_specs(registry)
    assert store.task_spec("A-TASK") is None


def test_revision_row_missing_or_corrupt_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_task_spec(_spec(), idempotency_key="missing", expected_revision=None, source="test")
    with store.connect() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM task_spec_revisions WHERE task_id='TEST-T001'")
    with pytest.raises(StateError, match="unknown TaskSpec revision"):
        store.task_spec("TEST-T001")
