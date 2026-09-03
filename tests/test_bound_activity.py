from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau.adapters import AdapterRegistry, Observation
from bureau.bound_activity import (
    BOUND_ACTIVITY_KIND,
    bound_activity_heartbeat,
    bound_activity_status,
    run_heartbeat_projection,
)
from bureau.core import Dispatcher, Registry, StateError, StateStore
from bureau.read_only_state import ReadOnlyStateStore
from bureau.v2 import coordinated_claim_status

_OLD_HEARTBEAT = "2000-01-01T00:00:00Z"


class _FakeAdapter:
    system = "grabowski-task"
    aliases: tuple[str, ...] = ()

    def __init__(self, state: str = "running", error: Exception | None = None):
        self.state = state
        self.error = error
        self.observed: list[str] = []

    def observe(self, external_id: str) -> Observation:
        self.observed.append(external_id)
        if self.error is not None:
            raise self.error
        return Observation(self.state, {"external_id": external_id})


def _adapter_registry(
    state: str = "running", error: Exception | None = None
) -> tuple[AdapterRegistry, _FakeAdapter]:
    adapter = _FakeAdapter(state, error)
    return AdapterRegistry([adapter]), adapter


def _claim(
    registry_factory,
    tmp_path: Path,
    *,
    worker_id: str = "worker-a",
    task_count: int = 1,
) -> tuple[Path, StateStore, dict]:
    root = registry_factory(task_count)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    run = Dispatcher(Registry.load(root), store).claim_next(worker_id, ("repository",))["run"]
    return root, store, run


def _activity_arguments(run: dict, activity_id: str = "activity-001") -> dict:
    arguments = {
        "activity_id": activity_id,
        "task_id": run["task_id"],
        "worker_id": run["worker_id"],
        "task_sha256": run["task_sha256"],
        "plan_sha256": run["plan_sha256"],
        "envelope_sha256": run["envelope_sha256"],
    }
    external_fields = {
        name: run[name]
        for name in (
            "external_system",
            "external_id",
            "external_state",
            "external_observed_at",
        )
    }
    if all(value is None for value in external_fields.values()):
        arguments["external_unbound"] = True
    else:
        arguments.update(external_fields)
    return arguments


def _set_old_heartbeat(store: StateStore, run: dict) -> None:
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET heartbeat_at=? WHERE run_id=?",
            (_OLD_HEARTBEAT, run["run_id"]),
        )
        connection.execute(
            "UPDATE workers SET heartbeat_at=? WHERE worker_id=?",
            (_OLD_HEARTBEAT, run["worker_id"]),
        )


def _bound_events(store: StateStore, run_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT run_id,activity_id,payload_json FROM events "
        "WHERE event_type='run-heartbeat' AND event_schema_version=1"
    )
    parameters: tuple[str, ...] = ()
    if run_id is not None:
        sql += " AND run_id=?"
        parameters = (run_id,)
    sql += " ORDER BY event_id"
    with store.connect() as connection:
        payloads = [
            {
                "run_id": row["run_id"],
                "activity_id": row["activity_id"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in connection.execute(sql, parameters)
        ]
    return [item for item in payloads if item["payload"].get("kind") == BOUND_ACTIVITY_KIND]


def _effects(store: StateStore, run: dict) -> dict:
    with store.connect() as connection:
        run_row = connection.execute(
            "SELECT heartbeat_at,updated_at FROM runs WHERE run_id=?", (run["run_id"],)
        ).fetchone()
        worker_heartbeat = connection.execute(
            "SELECT heartbeat_at FROM workers WHERE worker_id=?", (run["worker_id"],)
        ).fetchone()["heartbeat_at"]
        events = [
            tuple(row)
            for row in connection.execute(
                "SELECT event_id,run_id,event_type,event_schema_version,activity_id,"
                "payload_json,created_at "
                "FROM events ORDER BY event_id"
            )
        ]
    return {
        "run_heartbeat": run_row["heartbeat_at"],
        "run_updated_at": run_row["updated_at"],
        "worker_heartbeat": worker_heartbeat,
        "events": events,
    }


def _canonical_runtime_identity(root: Path) -> dict:
    resolved = str(root.resolve())
    commit = "a" * 40
    return {
        "schema_version": 1,
        "kind": "bureau_runtime_identity",
        "registry": {
            "available": True,
            "bureau_project": True,
            "role": "canonical-runtime-snapshot",
            "root": resolved,
            "head": commit,
            "origin_main": commit,
            "head_equals_origin_main": True,
            "dirty": False,
            "dirty_paths": [],
        },
        "manifest": {
            "available": True,
            "valid": True,
            "release_id": "release-test",
            "source_commit": commit,
            "canonical_registry": {
                "available": True,
                "valid": True,
                "root": resolved,
                "source_commit": commit,
                "tree_sha256": "b" * 64,
                "reasons": [],
            },
        },
        "compatibility": {
            "status": "canonical-read-only",
            "mutation_allowed": False,
            "reason_codes": ["canonical-registry-read-only"],
        },
        "state": {"available": False, "path": None, "schema_version": None},
    }


def test_active_bound_external_activity_refreshes_canonical_heartbeat(
    registry_factory, tmp_path: Path
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "external-001")
    _set_old_heartbeat(store, claimed)
    run = store.run(claimed["run_id"])
    adapter_registry, adapter = _adapter_registry()

    result = bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run),
        adapter_registry=adapter_registry,
    )
    events = _bound_events(store, run["run_id"])

    assert result["heartbeat_at"] != _OLD_HEARTBEAT
    assert result["bound_activity"] == {
        "activity_id": "activity-001",
        "status": "recorded",
        "heartbeat_at": result["heartbeat_at"],
        "liveness_truth": "runs.heartbeat_at",
        "event_type": "run-heartbeat",
        "payload": events[0]["payload"],
    }
    assert len(events) == 1
    assert events[0]["payload"]["source"] == "bound-activity"
    assert events[0]["payload"]["outcome"] == "succeeded"
    assert events[0]["activity_id"] == "activity-001"
    assert events[0]["payload"]["activity"]["external_binding"] == {
        "status": "bound",
        "external_system": "grabowski-task",
        "external_id": "external-001",
        "external_state": run["external_state"],
        "external_observed_at": run["external_observed_at"],
    }
    assert events[0]["payload"]["evidence"] == {
        "source": "adapter.observe",
        "external_system": "grabowski-task",
        "external_id": "external-001",
        "observed_state": "running",
        "observed_at": events[0]["payload"]["evidence"]["observed_at"],
    }
    assert adapter.observed == ["external-001"]
    assert store.replay_projection()["matches_current"] is True


def test_active_explicitly_unbound_activity_is_valid(registry_factory, tmp_path: Path) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)

    result = bound_activity_heartbeat(store, root, run["run_id"], **_activity_arguments(run))

    assert result["heartbeat_at"] != _OLD_HEARTBEAT
    event = _bound_events(store, run["run_id"])[0]["payload"]
    assert event["activity"]["external_binding"] == {"status": "explicitly-unbound"}
    assert event["evidence"] == {
        "source": "exact-run-binding",
        "binding_status": "explicitly-unbound",
    }


@pytest.mark.parametrize("observed_state", ["running", "succeeded"])
def test_external_activity_accepts_only_fresh_success_evidence(
    registry_factory, tmp_path: Path, observed_state: str
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "fresh-external")
    run = store.run(claimed["run_id"])
    _set_old_heartbeat(store, run)
    adapter_registry, adapter = _adapter_registry(observed_state)

    result = bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, f"fresh-{observed_state}"),
        adapter_registry=adapter_registry,
    )

    evidence = result["bound_activity"]["payload"]["evidence"]
    assert result["heartbeat_at"] != _OLD_HEARTBEAT
    assert evidence["source"] == "adapter.observe"
    assert evidence["observed_state"] == observed_state
    assert evidence["observed_at"]
    assert adapter.observed == ["fresh-external"]


@pytest.mark.parametrize(
    ("adapter_registry", "match"),
    [
        (None, "adapter registry is unavailable"),
        (AdapterRegistry(), "adapter 'grabowski-task' is unavailable"),
        (_adapter_registry(error=RuntimeError("observe broke"))[0], "observation failed"),
        (_adapter_registry("unknown")[0], "state 'unknown' is not active"),
        (_adapter_registry("failed")[0], "state 'failed' is not active"),
        (_adapter_registry("cancelled")[0], "state 'cancelled' is not active"),
        (_adapter_registry("interrupted")[0], "state 'interrupted' is not active"),
        (_adapter_registry("missing")[0], "state 'missing' is not active"),
    ],
)
def test_external_activity_fails_closed_without_fresh_success_evidence(
    registry_factory,
    tmp_path: Path,
    adapter_registry: AdapterRegistry | None,
    match: str,
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "stale-snapshot")
    run = store.run(claimed["run_id"])
    _set_old_heartbeat(store, run)
    before = _effects(store, run)

    with pytest.raises(StateError, match=match):
        bound_activity_heartbeat(
            store,
            root,
            run["run_id"],
            **_activity_arguments(run, "rejected-fresh-evidence"),
            adapter_registry=adapter_registry,
        )

    assert _effects(store, run) == before


def test_lease_only_or_incomplete_binding_cannot_refresh_heartbeat(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    arguments = _activity_arguments(run)
    arguments.pop("external_unbound")
    before = _effects(store, run)

    with pytest.raises(StateError, match="complete external binding snapshot"):
        bound_activity_heartbeat(store, root, run["run_id"], **arguments)

    assert _effects(store, run) == before

    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET external_system='grabowski-task' WHERE run_id=?",
            (run["run_id"],),
        )
    partial_before = _effects(store, run)
    with pytest.raises(StateError, match="external_system"):
        bound_activity_heartbeat(store, root, run["run_id"], **_activity_arguments(run))
    assert _effects(store, run) == partial_before


def test_terminal_run_rejects_bound_activity_without_reviving_it(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    with store.immediate() as connection:
        connection.execute("UPDATE runs SET state='completed' WHERE run_id=?", (run["run_id"],))
    before = _effects(store, run)

    with pytest.raises(StateError, match="not active"):
        bound_activity_heartbeat(store, root, run["run_id"], **_activity_arguments(run))

    assert _effects(store, run) == before
    assert store.run(run["run_id"])["state"] == "completed"


def test_wrong_worker_cannot_refresh_bound_activity(registry_factory, tmp_path: Path) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    arguments = _activity_arguments(run)
    arguments["worker_id"] = "worker-b"
    before = _effects(store, run)

    with pytest.raises(StateError, match="worker_id"):
        bound_activity_heartbeat(store, root, run["run_id"], **arguments)

    assert _effects(store, run) == before


@pytest.mark.parametrize(
    ("field", "stale_value"),
    [
        ("task_id", "BUR-TEST-001-T999"),
        ("task_sha256", "1" * 64),
        ("plan_sha256", "2" * 64),
        ("envelope_sha256", "3" * 64),
    ],
)
def test_stale_task_or_revision_binding_cannot_refresh_activity(
    registry_factory,
    tmp_path: Path,
    field: str,
    stale_value: str,
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    arguments = _activity_arguments(run)
    arguments[field] = stale_value
    before = _effects(store, run)

    with pytest.raises(StateError, match=field):
        bound_activity_heartbeat(store, root, run["run_id"], **arguments)

    assert _effects(store, run) == before


def test_current_authoritative_task_spec_drift_cannot_refresh_activity(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    arguments = _activity_arguments(run)
    store.import_registry_task_specs(Registry.load(root))
    current = store.task_spec(run["task_id"])
    assert current is not None
    revised = json.loads(json.dumps(current["spec"]))
    revised["title"] = "Authoritative revision changed after claim"
    store.put_task_spec(
        revised,
        idempotency_key="bound-activity-current-task-drift",
        expected_revision=current["revision"],
        source="test bound activity current TaskSpec drift",
    )
    before = _effects(store, run)

    with pytest.raises(StateError, match="current TaskSpec differs from run"):
        bound_activity_heartbeat(store, root, run["run_id"], **arguments)

    assert _effects(store, run) == before


def test_current_authoritative_plan_drift_cannot_refresh_activity(
    registry_factory, tmp_path: Path
) -> None:
    root = registry_factory(1)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text(encoding="utf-8"))
    initiative["current_plan"] = {
        "repository": "heimgewebe/bureau",
        "path": "docs/plans/bound-activity.md",
        "commit": "1" * 40,
        "document_sha256": "2" * 64,
    }
    initiative_path.write_text(json.dumps(initiative), encoding="utf-8")
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    run = Dispatcher(Registry.load(root), store).claim_next("worker-a", ("repository",))["run"]
    _set_old_heartbeat(store, run)
    arguments = _activity_arguments(run)

    initiative["current_plan"]["commit"] = "3" * 40
    initiative_path.write_text(json.dumps(initiative), encoding="utf-8")
    before = _effects(store, run)

    with pytest.raises(StateError, match="current plan differs from run"):
        bound_activity_heartbeat(store, root, run["run_id"], **arguments)

    assert _effects(store, run) == before


def test_stored_envelope_digest_drift_cannot_refresh_activity(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    arguments = _activity_arguments(run)
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET envelope_json='{}' WHERE run_id=?",
            (run["run_id"],),
        )
    before = _effects(store, run)

    with pytest.raises(StateError, match="run envelope digest mismatch"):
        bound_activity_heartbeat(store, root, run["run_id"], **arguments)

    assert _effects(store, run) == before


def test_external_binding_drift_requires_rebind(registry_factory, tmp_path: Path) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    bound = store.bind(claimed["run_id"], "grabowski-task", "external-001")
    arguments = _activity_arguments(bound)
    store.mark_dispatch_uncertain(bound["run_id"], "observer unavailable")
    before = _effects(store, bound)
    adapter_registry, _adapter = _adapter_registry()

    with pytest.raises(StateError, match=r"external_(state|observed_at)"):
        bound_activity_heartbeat(
            store,
            root,
            bound["run_id"],
            **arguments,
            adapter_registry=adapter_registry,
        )

    assert _effects(store, bound) == before


def test_exact_activity_replay_is_idempotent(registry_factory, tmp_path: Path) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    arguments = _activity_arguments(run, "stable-activity")
    first = bound_activity_heartbeat(store, root, run["run_id"], **arguments)
    before_replay = _effects(store, run)

    replay = bound_activity_heartbeat(store, root, run["run_id"], **arguments)

    assert replay["bound_activity"]["status"] == "replayed"
    assert replay["bound_activity"]["heartbeat_at"] == first["heartbeat_at"]
    assert _effects(store, run) == before_replay
    with store.connect() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT event_id FROM events WHERE activity_id=?",
            ("stable-activity",),
        ).fetchall()
    assert any("unique_bound_activity_id" in row["detail"] for row in plan)

    with store.immediate() as connection:
        connection.execute("UPDATE runs SET state='completed' WHERE run_id=?", (run["run_id"],))
    terminal_before = _effects(store, run)
    with pytest.raises(StateError, match="not active"):
        bound_activity_heartbeat(store, root, run["run_id"], **arguments)
    assert _effects(store, run) == terminal_before


def test_exact_external_activity_replay_never_reobserves(
    registry_factory, tmp_path: Path
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "replay-external")
    run = store.run(claimed["run_id"])
    arguments = _activity_arguments(run, "external-replay")
    first_registry, first_adapter = _adapter_registry("running")
    first = bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **arguments,
        adapter_registry=first_registry,
    )
    before_replay = _effects(store, run)

    replay_without_registry = bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **arguments,
        adapter_registry=None,
    )
    failing_registry, failing_adapter = _adapter_registry(
        error=RuntimeError("observe must not be called")
    )
    replay_with_failing_adapter = bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **arguments,
        adapter_registry=failing_registry,
    )

    assert replay_without_registry["bound_activity"]["status"] == "replayed"
    assert replay_with_failing_adapter["bound_activity"]["status"] == "replayed"
    assert replay_without_registry["bound_activity"]["payload"] == first["bound_activity"][
        "payload"
    ]
    assert replay_with_failing_adapter["bound_activity"]["payload"] == first[
        "bound_activity"
    ]["payload"]
    assert replay_without_registry["heartbeat_at"] == first["heartbeat_at"]
    assert replay_with_failing_adapter["heartbeat_at"] == first["heartbeat_at"]
    assert first_adapter.observed == ["replay-external"]
    assert failing_adapter.observed == []
    assert _effects(store, run) == before_replay


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("external_id", "caller-supplied-wrong-external"),
        ("worker_id", "caller-supplied-wrong-worker"),
        ("task_sha256", "f" * 64),
    ],
)
def test_wrong_external_or_revision_binding_never_observes(
    registry_factory,
    tmp_path: Path,
    field: str,
    wrong_value: str,
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "correct-external")
    run = store.run(claimed["run_id"])
    arguments = _activity_arguments(run, f"wrong-{field}")
    arguments[field] = wrong_value
    adapter_registry, adapter = _adapter_registry()
    before = _effects(store, run)

    with pytest.raises(StateError, match=field):
        bound_activity_heartbeat(
            store,
            root,
            run["run_id"],
            **arguments,
            adapter_registry=adapter_registry,
        )

    assert adapter.observed == []
    assert _effects(store, run) == before


def test_reused_activity_id_with_another_valid_binding_fails(
    registry_factory, tmp_path: Path
) -> None:
    root, store, first_run = _claim(registry_factory, tmp_path, task_count=2)
    second_run = Dispatcher(Registry.load(root), store).claim_next("worker-b", ("repository",))[
        "run"
    ]
    bound_activity_heartbeat(
        store,
        root,
        first_run["run_id"],
        **_activity_arguments(first_run, "globally-stable-activity"),
    )
    before = _effects(store, second_run)

    with pytest.raises(StateError, match="already used for another binding"):
        bound_activity_heartbeat(
            store,
            root,
            second_run["run_id"],
            **_activity_arguments(second_run, "globally-stable-activity"),
        )

    assert _effects(store, second_run) == before


def test_exact_bound_activity_readback_returns_complete_binding_without_effects(
    registry_factory, tmp_path: Path
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "readback-external-001")
    run = store.run(claimed["run_id"])
    arguments = _activity_arguments(run, "readback-activity")
    adapter_registry, _adapter = _adapter_registry("succeeded")
    mutation = bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **arguments,
        adapter_registry=adapter_registry,
    )
    before = _effects(store, run)
    read_store = ReadOnlyStateStore(store.path, store.state_root)
    with read_store.connect() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1

    status = bound_activity_status(read_store, run["run_id"], "readback-activity")

    assert status == {
        "schema_version": 1,
        "kind": "bureau_bound_activity_status",
        "status": "recorded",
        "run_id": run["run_id"],
        "activity_id": "readback-activity",
        "bound_activity": mutation["bound_activity"]["payload"],
    }
    assert status["bound_activity"]["activity"] == {
        "activity_id": "readback-activity",
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "worker_id": run["worker_id"],
        "task_sha256": run["task_sha256"],
        "plan_sha256": run["plan_sha256"],
        "envelope_sha256": run["envelope_sha256"],
        "external_binding": {
            "status": "bound",
            "external_system": "grabowski-task",
            "external_id": "readback-external-001",
            "external_state": run["external_state"],
            "external_observed_at": run["external_observed_at"],
        },
    }
    assert _effects(store, run) == before


def test_bound_activity_readback_reports_missing_exact_id_without_effects(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "recorded-activity"),
    )
    before = _effects(store, run)

    status = bound_activity_status(
        ReadOnlyStateStore(store.path, store.state_root),
        run["run_id"],
        "missing-activity",
    )

    assert status == {
        "schema_version": 1,
        "kind": "bureau_bound_activity_status",
        "status": "missing",
        "run_id": run["run_id"],
        "activity_id": "missing-activity",
        "bound_activity": None,
    }
    assert _effects(store, run) == before


def test_bound_activity_readback_does_not_accept_id_from_another_run(
    registry_factory, tmp_path: Path
) -> None:
    root, store, first_run = _claim(registry_factory, tmp_path, task_count=2)
    second_run = Dispatcher(Registry.load(root), store).claim_next("worker-b", ("repository",))[
        "run"
    ]
    bound_activity_heartbeat(
        store,
        root,
        first_run["run_id"],
        **_activity_arguments(first_run, "other-run-activity"),
    )
    before = _effects(store, second_run)

    with pytest.raises(StateError, match="run binding mismatch"):
        bound_activity_status(
            ReadOnlyStateStore(store.path, store.state_root),
            second_run["run_id"],
            "other-run-activity",
        )
    assert _effects(store, second_run) == before


@pytest.mark.parametrize(
    "corruption", ["kind", "digest", "external-snapshot", "evidence"]
)
def test_bound_activity_readback_fails_closed_on_malformed_matching_evidence(
    registry_factory,
    tmp_path: Path,
    corruption: str,
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "malformed-activity"),
    )
    with store.immediate() as connection:
        row = connection.execute(
            "SELECT event_id,payload_json FROM events WHERE activity_id=?",
            ("malformed-activity",),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        if corruption == "kind":
            payload["kind"] = "bureau.malformed_bound_activity_heartbeat"
        elif corruption == "digest":
            payload["activity"]["task_sha256"] = "not-a-digest"
        elif corruption == "external-snapshot":
            payload["activity"]["external_binding"]["external_id"] = "unexpected"
        else:
            payload["evidence"]["source"] = "caller-snapshot"
        connection.execute(
            "UPDATE events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, sort_keys=True), row["event_id"]),
        )
    before = _effects(store, run)

    with pytest.raises(StateError, match="bound activity"):
        bound_activity_status(
            ReadOnlyStateStore(store.path, store.state_root),
            run["run_id"],
            "malformed-activity",
        )

    assert _effects(store, run) == before


def test_bound_activity_readback_fails_closed_on_duplicate_matching_evidence(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "duplicate-activity"),
    )
    with store.immediate() as connection:
        connection.execute("DROP INDEX unique_bound_activity_id")
        connection.execute(
            "INSERT INTO events(run_id,event_type,event_schema_version,activity_id,"
            "payload_json,created_at) "
            "SELECT run_id,event_type,event_schema_version,activity_id,payload_json,created_at "
            "FROM events WHERE activity_id=?",
            ("duplicate-activity",),
        )
    before = _effects(store, run)

    with pytest.raises(StateError, match="evidence is ambiguous"):
        bound_activity_status(
            ReadOnlyStateStore(store.path, store.state_root),
            run["run_id"],
            "duplicate-activity",
        )

    assert _effects(store, run) == before


def test_bound_activity_index_enforces_global_uniqueness(registry_factory, tmp_path: Path) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "unique-activity"),
    )

    with store.immediate() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO events(run_id,event_type,event_schema_version,activity_id,"
            "payload_json,created_at) "
            "SELECT run_id,event_type,event_schema_version,activity_id,payload_json,created_at "
            "FROM events WHERE activity_id=?",
            ("unique-activity",),
        )


def test_heartbeat_projection_discovers_bound_activity_and_doctor_reports_it(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    mutation = bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "diagnostic-activity"),
    )

    projection = run_heartbeat_projection(
        ReadOnlyStateStore(store.path, store.state_root), run["run_id"]
    )
    doctor = Dispatcher(Registry.load(root), store).doctor()

    assert projection["status"] == "valid-bound-activity"
    assert projection["activity_id"] == "diagnostic-activity"
    assert projection["heartbeat_at"] == mutation["heartbeat_at"]
    assert projection["binding_source"] == "events.activity_id+exact-run-binding"
    assert projection["evidence_source"] == "exact-run-binding"
    assert doctor["run_heartbeat_projections"] == [projection]


def test_heartbeat_projection_classifies_legacy_heartbeat_as_normal(
    registry_factory, tmp_path: Path
) -> None:
    _root, store, run = _claim(registry_factory, tmp_path)

    refreshed = store.heartbeat(run["run_id"], run["worker_id"])
    projection = run_heartbeat_projection(
        ReadOnlyStateStore(store.path, store.state_root), run["run_id"]
    )

    assert projection["status"] == "normal"
    assert projection["canonical_source"] == "runs.heartbeat_at"
    assert projection["heartbeat_at"] == refreshed["heartbeat_at"]
    assert projection["activity_id"] is None


def test_malformed_bound_evidence_projects_fail_closed_rebind(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "projection-malformed"),
    )
    with store.immediate() as connection:
        connection.execute(
            "UPDATE events SET payload_json='{}' WHERE activity_id=?",
            ("projection-malformed",),
        )

    projection = run_heartbeat_projection(
        ReadOnlyStateStore(store.path, store.state_root), run["run_id"]
    )

    assert projection["status"] == "rebind-required"
    assert projection["canonical_source"] == "fail-closed"
    assert projection["activity_id"] == "projection-malformed"
    assert projection["rebind_required"] is True


def test_bound_projection_requires_current_exact_run_binding(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "projection-binding-drift"),
    )
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET task_sha256=? WHERE run_id=?",
            ("f" * 64, run["run_id"]),
        )

    projection = run_heartbeat_projection(
        ReadOnlyStateStore(store.path, store.state_root), run["run_id"]
    )

    assert projection["status"] == "rebind-required"
    assert projection["canonical_source"] == "fail-closed"
    assert "task_sha256" in projection["detail"]
    assert "task_sha256" in projection["reason"]
    assert projection["fail_closed"] is True


def test_run_and_runs_cli_include_common_heartbeat_projection(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "normal-run-output"),
    )
    identity = _canonical_runtime_identity(root)
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(identity)),
    )

    for command in (["run", run["run_id"]], ["runs"]):
        assert (
            bureau_cli.main(
                [
                    "--root",
                    str(root),
                    "--state-root",
                    str(store.state_root),
                    "--json",
                    *command,
                ]
            )
            == 0
        )
        value = json.loads(capsys.readouterr().out)["result"]
        projected_run = value[0] if isinstance(value, list) else value
        assert projected_run["heartbeat_projection"]["status"] == ("valid-bound-activity")
        assert projected_run["heartbeat_projection"]["activity_id"] == ("normal-run-output")


def test_reconcile_reports_projection_for_unobserved_bound_run(
    registry_factory, tmp_path: Path
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "reconcile-external")
    run = store.run(claimed["run_id"])
    adapter_registry, _adapter = _adapter_registry()
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "reconcile-activity"),
        adapter_registry=adapter_registry,
    )

    result = Dispatcher(Registry.load(root), store, AdapterRegistry()).reconcile(stale_after=999999)

    assert result["unobserved"][0]["run_id"] == run["run_id"]
    assert result["run_heartbeat_projections"][0]["status"] == ("valid-bound-activity")
    assert result["run_heartbeat_projections"][0]["activity_id"] == ("reconcile-activity")


def test_reconcile_reports_projection_for_unchanged_active_run(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "reconcile-unchanged-activity"),
    )

    result = Dispatcher(Registry.load(root), store).reconcile(stale_after=999999)

    assert result["orphaned"] == []
    assert result["recovered"] == []
    assert result["refreshed"] == []
    assert result["verifying"] == []
    assert result["terminal"] == []
    assert result["unobserved"] == []
    assert result["run_heartbeat_projections"][0]["run_id"] == run["run_id"]
    assert result["run_heartbeat_projections"][0]["status"] == (
        "valid-bound-activity"
    )
    assert result["run_heartbeat_projections"][0]["activity_id"] == (
        "reconcile-unchanged-activity"
    )


def test_claim_coordination_status_option_appends_readback_and_absence_is_compatible(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    mutation = bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "cli-readback-activity"),
    )
    expected_without_activity = coordinated_claim_status(
        ReadOnlyStateStore(store.path, store.state_root),
        run["run_id"],
    )
    before = _effects(store, run)
    identity = _canonical_runtime_identity(root)
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(identity)),
    )
    parsed = bureau_cli.parser().parse_args(
        ["claim-coordination-status", run["run_id"], "--activity-id", "cli-readback-activity"]
    )
    assert parsed.activity_id == "cli-readback-activity"
    assert bureau_cli._command_mutates(parsed) is False
    assert bureau_cli._command_effect_scope(parsed) == "read_only"

    exit_code = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(store.state_root),
            "--json",
            "claim-coordination-status",
            run["run_id"],
            "--activity-id",
            "cli-readback-activity",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"]["bound_activity"] == {
        "schema_version": 1,
        "kind": "bureau_bound_activity_status",
        "status": "recorded",
        "run_id": run["run_id"],
        "activity_id": "cli-readback-activity",
        "bound_activity": mutation["bound_activity"]["payload"],
    }
    assert output["result"]["run"]["heartbeat_projection"]["activity_id"] == (
        "cli-readback-activity"
    )
    assert _effects(store, run) == before

    exit_code = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(store.state_root),
            "--json",
            "claim-coordination-status",
            run["run_id"],
        ]
    )

    assert exit_code == 0
    output_without_activity_bytes = capsys.readouterr().out
    runtime_identity = bureau_cli._CLI_RUNTIME_IDENTITY
    assert runtime_identity is not None
    assert output_without_activity_bytes == (
        json.dumps(
            {
                "schema_version": 1,
                "runtime_identity": runtime_identity,
                "result": expected_without_activity,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )
    output_without_activity = json.loads(output_without_activity_bytes)
    assert output_without_activity["result"] == expected_without_activity
    assert _effects(store, run) == before


def test_cli_bound_activity_mode_records_explicitly_unbound_activity(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    identity = _canonical_runtime_identity(root)
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(identity)),
    )

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(store.state_root),
            "--json",
            "heartbeat",
            run["run_id"],
            "--bound-activity",
            "--activity-id",
            "cli-activity",
            "--task-id",
            run["task_id"],
            "--worker",
            run["worker_id"],
            "--task-sha256",
            run["task_sha256"],
            "--plan-sha256",
            run["plan_sha256"],
            "--envelope-sha256",
            run["envelope_sha256"],
            "--external-unbound",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"]["bound_activity"]["status"] == "recorded"
    assert output["result"]["heartbeat_at"] != _OLD_HEARTBEAT
    assert len(_bound_events(store, run["run_id"])) == 1


def test_cli_bound_external_activity_uses_registry_observation_without_flag_changes(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "cli-external")
    run = store.run(claimed["run_id"])
    _set_old_heartbeat(store, run)
    adapter_registry, adapter = _adapter_registry("succeeded")
    identity = _canonical_runtime_identity(root)
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(identity)),
    )
    monkeypatch.setattr(bureau_cli, "adapters", lambda _args: adapter_registry)

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(store.state_root),
            "--json",
            "heartbeat",
            run["run_id"],
            "--bound-activity",
            "--activity-id",
            "cli-external-activity",
            "--task-id",
            run["task_id"],
            "--worker",
            run["worker_id"],
            "--task-sha256",
            run["task_sha256"],
            "--plan-sha256",
            run["plan_sha256"],
            "--envelope-sha256",
            run["envelope_sha256"],
            "--external-system",
            run["external_system"],
            "--external-id",
            run["external_id"],
            "--external-state",
            run["external_state"],
            "--external-observed-at",
            run["external_observed_at"],
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    evidence = output["result"]["bound_activity"]["payload"]["evidence"]
    assert evidence["source"] == "adapter.observe"
    assert evidence["observed_state"] == "succeeded"
    assert evidence["observed_at"] != run["external_observed_at"]
    assert adapter.observed == ["cli-external"]


def test_cli_empty_bound_argument_cannot_fall_back_to_legacy_heartbeat(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    before = _effects(store, run)
    identity = _canonical_runtime_identity(root)
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(identity)),
    )

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(store.state_root),
            "--json",
            "heartbeat",
            run["run_id"],
            "--worker",
            run["worker_id"],
            "--activity-id",
            "",
        ]
    )

    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert "bound activity arguments require --bound-activity" in output["result"]["detail"]
    assert _effects(store, run) == before


def test_legacy_cli_heartbeat_remains_compatible(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    _set_old_heartbeat(store, run)
    identity = _canonical_runtime_identity(root)
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(identity)),
    )

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(store.state_root),
            "--json",
            "heartbeat",
            run["run_id"],
            "--worker",
            run["worker_id"],
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"]["heartbeat_at"] != _OLD_HEARTBEAT
    assert "bound_activity" not in output["result"]
    with store.connect() as connection:
        payloads = [
            json.loads(row["payload_json"])
            for row in connection.execute(
                "SELECT payload_json FROM events "
                "WHERE run_id=? AND event_type='run-heartbeat' ORDER BY event_id",
                (run["run_id"],),
            )
        ]
    assert payloads == [{}]


@pytest.mark.parametrize(
    ("state", "expected_bucket"),
    [("running", "refreshed"), ("succeeded", "verifying")],
)
def test_reconcile_fresh_observation_supersedes_bound_heartbeat(
    registry_factory, tmp_path: Path, state: str, expected_bucket: str
) -> None:
    root, store, claimed = _claim(registry_factory, tmp_path)
    store.bind(claimed["run_id"], "grabowski-task", "reconcile-supersede")
    run = store.run(claimed["run_id"])
    adapter_registry, _adapter = _adapter_registry(state)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "reconcile-superseded-activity"),
        adapter_registry=adapter_registry,
    )

    result = Dispatcher(Registry.load(root), store, adapter_registry).reconcile(stale_after=999999)

    assert run["run_id"] in result[expected_bucket]
    projection = result["run_heartbeat_projections"][0]
    assert projection["status"] == "normal"
    assert projection["fail_closed"] is False
    assert projection["activity_id"] is None
    with store.connect() as connection:
        event = connection.execute(
            "SELECT activity_id,payload_json FROM events "
            "WHERE run_id=? AND event_type='run-heartbeat' ORDER BY event_id DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
    assert event["activity_id"] is None
    assert json.loads(event["payload_json"])["source"] == "reconcile-external-observation"


def test_doctor_fail_closed_projection_is_unhealthy(
    registry_factory, tmp_path: Path
) -> None:
    root, store, run = _claim(registry_factory, tmp_path)
    bound_activity_heartbeat(
        store,
        root,
        run["run_id"],
        **_activity_arguments(run, "doctor-fail-closed"),
    )
    with store.immediate() as connection:
        connection.execute(
            "UPDATE events SET payload_json='{}' WHERE activity_id=?",
            ("doctor-fail-closed",),
        )

    doctor = Dispatcher(Registry.load(root), store).doctor()

    assert doctor["healthy"] is False
    assert doctor["heartbeat_projection_findings"]
    finding = doctor["heartbeat_projection_findings"][0]
    assert finding["run_id"] == run["run_id"]
    assert finding["fail_closed"] is True
    assert doctor["runtime_truth"]["healthy"] is False


def test_read_only_projection_handles_pre_v4_event_table(tmp_path: Path) -> None:
    state_root = tmp_path / "legacy-state"
    state_root.mkdir()
    database = state_root / "bureau.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE runs(run_id TEXT PRIMARY KEY, heartbeat_at TEXT);
        CREATE TABLE events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,event_type TEXT,
            payload_json TEXT,created_at TEXT
        );
        INSERT INTO runs(run_id,heartbeat_at) VALUES('legacy-run','2026-08-01T00:00:00Z');
        INSERT INTO events(run_id,event_type,payload_json,created_at)
        VALUES('legacy-run','run-heartbeat','{}','2026-08-01T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()

    projection = run_heartbeat_projection(
        ReadOnlyStateStore(database, state_root), "legacy-run"
    )

    assert projection["status"] == "normal"
    assert projection["fail_closed"] is False
    assert projection["heartbeat_at"] == "2026-08-01T00:00:00Z"
