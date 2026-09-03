from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bureau import task_specs
from bureau.core import (
    Registry,
    StateStore,
    task_revision_sha256,
)
from bureau.core import (
    plan_sha256 as initiative_plan_sha256,
)
from bureau.legacy import StateError, canonical_json, sha256_json, utc_now
from bureau.repository_identity_rebind import (
    _candidates,
    apply_plan,
    build_plan,
    plan_sha256,
)

OLD_RESOURCE = "repo.old"
NEW_RESOURCE = "repo.new"


def _acceptance(task_id: str, *, legacy: bool) -> list[dict[str, object]]:
    if legacy:
        return [{"id": "legacy", "assertion": f"legacy proof for {task_id}"}]
    return [
        {
            "id": "proof",
            "assertion": f"typed proof for {task_id}",
            "evidence_type": "object",
            "verifier": "manual_observation",
            "verifier_config": {"observation_scope": f"test:{task_id}:proof"},
        }
    ]


def _task(task_id: str, old_path: str, *, legacy: bool) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": task_id,
        "initiative": "TEST-REBIND-V1",
        "title": f"Task {task_id}",
        "state": "ready",
        "depends_on": [],
        "required_capabilities": ["repository"],
        "priority": {"lane": "now", "rank": 1},
        "execution": {
            "mode": "interactive-agent",
            "policy": "autonomous",
            "working_repository": old_path,
            "grabowski_resources": [
                f"repo:{old_path}",
                f"path:{old_path}/apps/web",
                f"repo:{old_path}:operation:test-scope",
            ],
        },
        "claims": [
            {
                "resource": OLD_RESOURCE,
                "mode": "write",
                "isolation": "worktree",
            }
        ],
        "acceptance": _acceptance(task_id, legacy=legacy),
        "metadata": {"keep": {"task": task_id, "value": [1, 2, 3]}},
    }


def _registry_root(
    tmp_path: Path,
    *,
    task_ids: tuple[str, ...] = ("TASK-A", "TASK-B"),
    legacy: bool = True,
) -> tuple[Path, Path, Path]:
    root = tmp_path / "registry"
    for folder in ("registry/initiatives", "registry/tasks", "registry/resources", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parents[1]
    for schema in (source / "schemas").glob("*.json"):
        shutil.copy2(schema, root / "schemas" / schema.name)

    old_path = tmp_path / "old-repository"
    new_path = tmp_path / "new-repository"
    old_path.mkdir()
    new_path.mkdir()

    initiative = {
        "schema_version": 1,
        "id": "TEST-REBIND-V1",
        "title": "Repository identity rebind test",
        "state": "active",
        "commitment": "now",
        "goal": "test repository identity rebind",
        "completion": ["done"],
        "parallelism": {"max_active_tasks": 20},
    }
    (root / "registry/initiatives/main.json").write_text(
        json.dumps(initiative), encoding="utf-8"
    )

    resources = [
        {"schema_version": 1, "id": "root", "type": "group"},
        {
            "schema_version": 1,
            "id": OLD_RESOURCE,
            "type": "git-repository",
            "parent": "root",
            "path": str(old_path),
            "github_slug": "example/old",
            "grabowski_key": f"repo:{old_path}",
            "criticality": "essential",
        },
        {
            "schema_version": 1,
            "id": NEW_RESOURCE,
            "type": "git-repository",
            "parent": "root",
            "path": str(new_path),
            "github_slug": "example/new",
            "grabowski_key": f"repo:{new_path}",
            "criticality": "essential",
        },
    ]
    for index, resource in enumerate(resources):
        (root / f"registry/resources/{index}.json").write_text(
            json.dumps(resource), encoding="utf-8"
        )

    for index, task_id in enumerate(task_ids):
        task = _task(task_id, str(old_path), legacy=legacy)
        task["priority"]["rank"] = index + 1
        (root / f"registry/tasks/{task_id}.json").write_text(
            json.dumps(task), encoding="utf-8"
        )
    (root / "registry/queue.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "queue_policy": "skip-blocked",
                "lanes": {"now": list(task_ids), "next": [], "later": []},
            }
        ),
        encoding="utf-8",
    )
    return root, old_path, new_path


def _store(tmp_path: Path, registry: Registry) -> StateStore:
    state_root = tmp_path / "state"
    state_root.mkdir()
    store = StateStore(state_root / "bureau.sqlite3", state_root=state_root)
    imported = store.import_registry_task_specs(registry)
    assert imported["imported"] == len(registry.tasks)
    return store


def _insert_active_run(store: StateStore, task_id: str, run_id: str) -> None:
    store.register_worker(f"worker-{run_id}", "interactive-agent", ("repository",))
    current = store.task_spec(task_id)
    assert current is not None
    now = utc_now()
    envelope = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "worker_id": f"worker-{run_id}",
    }
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO runs(run_id,task_id,worker_id,attempt,state,task_sha256,"
            "plan_sha256,envelope_json,envelope_sha256,created_at,updated_at,heartbeat_at) "
            "VALUES(?,?,?,1,'assigned',?,'',?,?,?,?,?)",
            (
                run_id,
                task_id,
                f"worker-{run_id}",
                current["spec_sha256"],
                canonical_json(envelope),
                sha256_json(envelope),
                now,
                now,
                now,
            ),
        )


def test_legacy_acceptance_rebind_is_atomic_replayable_and_idempotent(tmp_path: Path) -> None:
    root, old_path, new_path = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    before_acceptance = {
        task_id: store.task_spec(task_id)["spec"]["acceptance"]
        for task_id in registry.tasks
    }

    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )

    assert plan["summary"] == {
        "migration_items": 2,
        "excluded_active_tasks": 0,
        "changed_paths": 10,
    }
    assert all(item["acceptance_diagnostics_sha256"] for item in plan["items"])

    result = apply_plan(
        registry,
        store,
        plan,
        expected_plan_sha256=plan["plan_sha256"],
    )

    assert result["status"] == "applied"
    assert result["changed"] is True
    assert result["migration_items"] == 2
    for task_id in registry.tasks:
        current = store.task_spec(task_id)
        assert current is not None
        assert current["revision"] == 2
        assert current["spec"]["acceptance"] == before_acceptance[task_id]
        assert current["spec"]["claims"][0]["resource"] == NEW_RESOURCE
        assert current["spec"]["execution"]["working_repository"] == str(new_path)
        assert current["spec"]["execution"]["grabowski_resources"] == [
            f"repo:{new_path}",
            f"path:{new_path}/apps/web",
            f"repo:{new_path}:operation:test-scope",
        ]
        assert str(old_path) not in canonical_json(current["spec"])
    replay = store.replay_projection()
    assert replay["task_specs"]["matches_current"] is True

    second = apply_plan(
        registry,
        store,
        plan,
        expected_plan_sha256=plan["plan_sha256"],
    )
    assert second["status"] == "already-applied"
    assert second["changed"] is False
    assert {store.task_spec(task_id)["revision"] for task_id in registry.tasks} == {2}


def test_stale_plan_without_receipts_is_rejected(tmp_path: Path) -> None:
    root, _, _ = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )
    plan["generated_at"] = "2000-01-01T00:00:00Z"
    plan["plan_sha256"] = plan_sha256(plan)

    with pytest.raises(StateError, match="plan is stale"):
        apply_plan(
            registry,
            store,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
            max_age_seconds=1,
        )

    assert {store.task_spec(task_id)["revision"] for task_id in registry.tasks} == {1}


def test_complete_receipt_replay_is_allowed_after_plan_expiry(tmp_path: Path) -> None:
    root, _, _ = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )
    plan["generated_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=2)
    ).isoformat().replace("+00:00", "Z")
    plan["plan_sha256"] = plan_sha256(plan)
    original_plan_sha256 = plan["plan_sha256"]
    first = apply_plan(
        registry,
        store,
        plan,
        expected_plan_sha256=original_plan_sha256,
        max_age_seconds=10,
    )
    assert first["status"] == "applied"

    replay = apply_plan(
        registry,
        store,
        plan,
        expected_plan_sha256=original_plan_sha256,
        max_age_seconds=1,
    )

    assert plan["plan_sha256"] == original_plan_sha256
    assert replay["status"] == "already-applied"
    assert replay["changed"] is False
    assert {store.task_spec(task_id)["revision"] for task_id in registry.tasks} == {2}


@pytest.mark.parametrize("target_suffix", ("-new", "/archive"))
def test_rebind_allows_new_repository_path_containing_old_path(
    tmp_path: Path, target_suffix: str
) -> None:
    root, old_path, _ = _registry_root(tmp_path, task_ids=("TASK-A",), legacy=True)
    new_path = Path(str(old_path) + target_suffix)
    new_path.mkdir()
    resource_path = root / "registry/resources/2.json"
    resource = json.loads(resource_path.read_text(encoding="utf-8"))
    resource["path"] = str(new_path)
    resource["grabowski_key"] = f"repo:{new_path}"
    resource_path.write_text(json.dumps(resource), encoding="utf-8")

    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )
    result = apply_plan(
        registry,
        store,
        plan,
        expected_plan_sha256=plan["plan_sha256"],
    )

    assert result["status"] == "applied"
    current = store.task_spec("TASK-A")
    assert current is not None
    assert current["spec"]["execution"]["working_repository"] == str(new_path)
    assert current["spec"]["execution"]["grabowski_resources"] == [
        f"repo:{new_path}",
        f"path:{new_path}/apps/web",
        f"repo:{new_path}:operation:test-scope",
    ]
    with store.connect() as connection:
        assert _candidates(
            connection,
            registry,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_path=str(old_path),
            new_path=str(new_path),
        ) == {}


def test_preview_rejects_preexisting_target_repository_claim(tmp_path: Path) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["claims"].append(
        {
            "resource": NEW_RESOURCE,
            "mode": "read",
            "isolation": "none",
        }
    )

    with pytest.raises(
        task_specs.TaskSpecError,
        match="refuses a pre-existing target repository claim",
    ):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


def test_preview_preserves_already_rebound_descendant_resource_path(
    tmp_path: Path,
) -> None:
    _, old_path, _ = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    new_path = old_path / "archive"
    new_path.mkdir()
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["execution"]["working_repository"] = str(new_path)
    spec["execution"]["grabowski_resources"] = [
        f"repo:{old_path}",
        f"path:{new_path}",
        f"path:{new_path}/cache",
        f"repo:{old_path}:operation:test-scope",
    ]

    preview = task_specs.preview_repository_identity_rebind(
        spec,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
        old_repository_path=str(old_path),
        new_repository_path=str(new_path),
    )

    assert preview["spec"]["execution"]["working_repository"] == str(new_path)
    assert preview["spec"]["execution"]["grabowski_resources"] == [
        f"repo:{new_path}",
        f"path:{new_path}",
        f"path:{new_path}/cache",
        f"repo:{new_path}:operation:test-scope",
    ]
    assert "/execution/grabowski_resources/1" not in preview["changed_paths"]
    assert "/execution/grabowski_resources/2" not in preview["changed_paths"]


def test_preview_rejects_execution_old_path_when_new_path_is_ancestor(
    tmp_path: Path,
) -> None:
    new_path = tmp_path / "repos" / "app"
    old_path = new_path / "archive"
    old_path.mkdir(parents=True)
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["execution"]["argv"] = ["tool", f"--repo={old_path}"]

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


def test_preview_rewrites_exact_old_path_key_when_new_path_is_ancestor(
    tmp_path: Path,
) -> None:
    new_path = tmp_path / "repos" / "app"
    old_path = new_path / "archive"
    old_path.mkdir(parents=True)
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["execution"]["grabowski_resources"] = [
        f"repo:{old_path}",
        f"path:{old_path}",
        f"path:{old_path}/cache",
        f"path:{new_path}/already-rebound",
        f"repo:{old_path}:operation:test-scope",
    ]

    preview = task_specs.preview_repository_identity_rebind(
        spec,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
        old_repository_path=str(old_path),
        new_repository_path=str(new_path),
    )

    assert preview["spec"]["execution"]["grabowski_resources"] == [
        f"repo:{new_path}",
        f"path:{new_path}",
        f"path:{new_path}/cache",
        f"path:{new_path}/already-rebound",
        f"repo:{new_path}:operation:test-scope",
    ]
    assert "/execution/grabowski_resources/1" in preview["changed_paths"]
    assert "/execution/grabowski_resources/2" in preview["changed_paths"]
    assert "/execution/grabowski_resources/3" not in preview["changed_paths"]


@pytest.mark.parametrize("stale_field", ("task_sha256", "plan_sha256"))
def test_stale_terminal_overlay_does_not_hide_old_binding(
    tmp_path: Path, stale_field: str
) -> None:
    root, _, _ = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    current = store.task_spec("TASK-A")
    assert current is not None
    bindings = {
        "task_sha256": task_revision_sha256(current["spec"]),
        "plan_sha256": initiative_plan_sha256(registry, "TEST-REBIND-V1"),
    }
    bindings[stale_field] = "0" * 64
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO task_status("
            "task_id,task_sha256,plan_sha256,state,updated_at"
            ") VALUES(?,?,?,?,?)",
            (
                "TASK-A",
                bindings["task_sha256"],
                bindings["plan_sha256"],
                "verified",
                utc_now(),
            ),
        )

    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )

    items = {item["task_id"]: item for item in plan["items"]}
    assert set(items) == {"TASK-A", "TASK-B"}
    assert items["TASK-A"]["effective_state"] == "stale"

    result = apply_plan(
        registry,
        store,
        plan,
        expected_plan_sha256=plan["plan_sha256"],
    )
    assert result["status"] == "applied"
    assert result["migration_items"] == 2
    assert store.task_spec("TASK-A")["spec"]["claims"][0]["resource"] == NEW_RESOURCE


def test_plan_requires_exact_live_active_task_exclusion(tmp_path: Path) -> None:
    root, old_path, _ = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    _insert_active_run(store, "TASK-B", "RUN-B")

    with pytest.raises(StateError, match="active-task exclusions do not exactly match"):
        build_plan(
            registry,
            store,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
        )

    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
        excluded_active_task_ids=("TASK-B",),
    )
    assert plan["summary"]["migration_items"] == 1
    assert plan["excluded_active_tasks"] == [
        {
            "task_id": "TASK-B",
            "run_id": "RUN-B",
            "run_state": "assigned",
            "expected_revision": 1,
            "expected_spec_sha256": store.task_spec("TASK-B")["spec_sha256"],
        }
    ]

    result = apply_plan(
        registry,
        store,
        plan,
        expected_plan_sha256=plan["plan_sha256"],
    )
    assert result["status"] == "applied"
    assert store.task_spec("TASK-A")["revision"] == 2
    assert store.task_spec("TASK-B")["revision"] == 1
    assert store.task_spec("TASK-B")["spec"]["claims"][0]["resource"] == OLD_RESOURCE
    assert store.task_spec("TASK-B")["spec"]["execution"]["working_repository"] == str(
        old_path
    )



@pytest.mark.parametrize("residual_kind", ("path", "resource"))
def test_active_exclusion_rejects_unapproved_old_binding_residue_at_plan(
    tmp_path: Path, residual_kind: str
) -> None:
    root, old_path, _ = _registry_root(tmp_path, legacy=True)
    task_path = root / "registry/tasks/TASK-B.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["metadata"]["unapproved_old_binding"] = (
        str(old_path) if residual_kind == "path" else OLD_RESOURCE
    )
    task_path.write_text(json.dumps(task), encoding="utf-8")

    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    _insert_active_run(store, "TASK-B", "RUN-B")

    with pytest.raises(StateError, match="left old technical bindings"):
        build_plan(
            registry,
            store,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            excluded_active_task_ids=("TASK-B",),
        )

    assert store.task_spec("TASK-A")["revision"] == 1
    assert store.task_spec("TASK-B")["revision"] == 1


@pytest.mark.parametrize("residual_kind", ("path", "resource"))
def test_apply_revalidates_legacy_plan_exclusion_preview_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, residual_kind: str
) -> None:
    root, old_path, _ = _registry_root(tmp_path, legacy=True)
    task_path = root / "registry/tasks/TASK-B.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["metadata"]["unapproved_old_binding"] = (
        str(old_path) if residual_kind == "path" else OLD_RESOURCE
    )
    task_path.write_text(json.dumps(task), encoding="utf-8")

    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    _insert_active_run(store, "TASK-B", "RUN-B")
    original_preview = task_specs.preview_repository_identity_rebind

    def legacy_preview(spec, **kwargs):
        if spec.get("id") == "TASK-B":
            sanitized = json.loads(canonical_json(spec))
            sanitized["metadata"].pop("unapproved_old_binding", None)
            return original_preview(sanitized, **kwargs)
        return original_preview(spec, **kwargs)

    monkeypatch.setattr(task_specs, "preview_repository_identity_rebind", legacy_preview)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
        excluded_active_task_ids=("TASK-B",),
    )
    monkeypatch.setattr(task_specs, "preview_repository_identity_rebind", original_preview)

    with pytest.raises(StateError, match="left old technical bindings"):
        apply_plan(
            registry,
            store,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
        )

    assert store.task_spec("TASK-A")["revision"] == 1
    assert store.task_spec("TASK-B")["revision"] == 1
    for item in plan["items"]:
        assert store.task_spec_mutation_receipt(item["idempotency_key"]) is None


def test_single_task_revision_drift_rolls_back_entire_batch(tmp_path: Path) -> None:
    root, _, _ = _registry_root(tmp_path, legacy=False)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )

    drifted = json.loads(canonical_json(store.task_spec("TASK-B")["spec"]))
    drifted["metadata"]["drift"] = "after-plan"
    update = store.put_task_spec(
        drifted,
        idempotency_key="unrelated-drift-before-rebind",
        expected_revision=1,
        source="test",
    )
    assert update["revision"] == 2

    with pytest.raises(StateError, match="TaskSpec projection root changed"):
        apply_plan(
            registry,
            store,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
        )

    assert store.task_spec("TASK-A")["revision"] == 1
    assert store.task_spec("TASK-A")["spec"]["claims"][0]["resource"] == OLD_RESOURCE
    assert store.task_spec("TASK-B")["revision"] == 2
    assert store.task_spec("TASK-B")["spec"]["metadata"]["drift"] == "after-plan"
    for item in plan["items"]:
        assert store.task_spec_mutation_receipt(item["idempotency_key"]) is None


def test_failure_after_first_internal_write_rolls_back_entire_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )
    original = task_specs._put_validated_material
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise task_specs.TaskSpecError("synthetic second-write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(task_specs, "_put_validated_material", fail_on_second)

    with pytest.raises(StateError, match="synthetic second-write failure"):
        apply_plan(
            registry,
            store,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
        )

    assert calls == 2
    assert store.task_spec("TASK-A")["revision"] == 1
    assert store.task_spec("TASK-B")["revision"] == 1
    for item in plan["items"]:
        assert store.task_spec_mutation_receipt(item["idempotency_key"]) is None
    assert store.replay_projection()["task_specs"]["matches_current"] is True


def test_active_run_created_after_plan_blocks_without_any_revision(tmp_path: Path) -> None:
    root, _, _ = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )
    _insert_active_run(store, "TASK-B", "RUN-AFTER-PLAN")

    with pytest.raises(StateError, match="acquired active runs: TASK-B"):
        apply_plan(
            registry,
            store,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
        )

    assert store.task_spec("TASK-A")["revision"] == 1
    assert store.task_spec("TASK-B")["revision"] == 1


def test_resource_binding_drift_invalidates_plan_before_state_mutation(tmp_path: Path) -> None:
    root, _, new_path = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )

    new_resource_path = root / "registry/resources/2.json"
    changed = json.loads(new_resource_path.read_text(encoding="utf-8"))
    changed["github_slug"] = "example/new-after-plan"
    new_resource_path.write_text(json.dumps(changed), encoding="utf-8")
    drifted_registry = Registry.load(root)

    with pytest.raises(StateError, match="resource binding changed"):
        apply_plan(
            drifted_registry,
            store,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
        )

    for task_id in registry.tasks:
        current = store.task_spec(task_id)
        assert current["revision"] == 1
        assert current["spec"]["claims"][0]["resource"] == OLD_RESOURCE
        assert current["spec"]["execution"]["working_repository"] != str(new_path)


def test_regular_taskspec_put_still_rejects_legacy_acceptance(tmp_path: Path) -> None:
    root, _, _ = _registry_root(tmp_path, task_ids=("TASK-A",), legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    current = store.task_spec("TASK-A")
    assert current is not None

    legacy_changed = json.loads(canonical_json(current["spec"]))
    legacy_changed["title"] = "ordinary writer must not bless legacy acceptance"
    with pytest.raises(StateError, match="evidence_type"):
        store.put_task_spec(
            legacy_changed,
            idempotency_key="ordinary-legacy-rewrite",
            expected_revision=current["revision"],
            source="ordinary-writer",
        )

    assert store.task_spec("TASK-A")["revision"] == 1


@pytest.mark.parametrize(
    "residual_kind",
    (
        "path",
        "resource",
        "file-uri",
        "file-uri-query",
        "file-uri-fragment",
        "uri-query-pair",
        "file-uri-host",
    ),
)
def test_plan_rejects_old_binding_only_in_unapproved_metadata(
    tmp_path: Path, residual_kind: str
) -> None:
    root, old_path, new_path = _registry_root(tmp_path, legacy=True)
    task_path = root / "registry/tasks/TASK-B.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["claims"][0]["resource"] = NEW_RESOURCE
    task["execution"]["working_repository"] = str(new_path)
    task["execution"]["grabowski_resources"] = [
        f"repo:{new_path}",
        f"path:{new_path}/apps/web",
        f"repo:{new_path}:operation:test-scope",
    ]
    residue_values = {
        "path": str(old_path),
        "resource": OLD_RESOURCE,
        "file-uri": f"file://{old_path}",
        "file-uri-query": f"file://{old_path}?rev=main",
        "file-uri-fragment": f"file://{old_path}#checkout",
        "uri-query-pair": f"tool://open?path={old_path}&rev=main",
        "file-uri-host": f"file://localhost{old_path}",
    }
    task["metadata"]["unapproved_old_binding"] = residue_values[residual_kind]
    task_path.write_text(json.dumps(task), encoding="utf-8")

    registry = Registry.load(root)
    store = _store(tmp_path, registry)

    with pytest.raises(
        StateError,
        match=r"old technical bindings outside approved rebind surfaces for task TASK-B",
    ):
        build_plan(
            registry,
            store,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
        )

    assert store.task_spec("TASK-A")["revision"] == 1
    assert store.task_spec("TASK-B")["revision"] == 1


def test_preview_rejects_old_repository_path_in_mapping_key(tmp_path: Path) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["execution"]["operation_parameters"] = {
        "mounts": {str(old_path): "/work"}
    }

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


@pytest.mark.parametrize(
    "placement", ("argv", "operation-parameter-key", "operation-parameter-value")
)
def test_preview_rejects_embedded_old_resource_id_in_execution(
    tmp_path: Path, placement: str
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    if placement == "argv":
        spec["execution"]["argv"] = ["tool", f"--resource={OLD_RESOURCE}"]
    elif placement == "operation-parameter-key":
        spec["execution"]["operation_parameters"] = {
            f"--resource={OLD_RESOURCE}": "enabled"
        }
    else:
        spec["execution"]["operation_parameters"] = {
            "resource_arg": f"--resource={OLD_RESOURCE}"
        }

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


@pytest.mark.parametrize(
    "placement", ("argv", "operation-parameter-key", "operation-parameter-value")
)
def test_preview_rejects_percent_encoded_old_resource_id_in_execution(
    tmp_path: Path, placement: str
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    encoded_resource = OLD_RESOURCE.replace(".", "%2E")
    encoded_binding = f"tool://open?resource={encoded_resource}"
    if placement == "argv":
        spec["execution"]["argv"] = ["tool", encoded_binding]
    elif placement == "operation-parameter-key":
        spec["execution"]["operation_parameters"] = {encoded_binding: "enabled"}
    else:
        spec["execution"]["operation_parameters"] = {
            "resource_arg": encoded_binding
        }

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


def test_preview_allows_target_resource_id_extending_old_id_with_colon(
    tmp_path: Path,
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    new_resource_id = f"{OLD_RESOURCE}:archive"
    spec["metadata"]["target_resource"] = new_resource_id

    result = task_specs.preview_repository_identity_rebind(
        spec,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=new_resource_id,
        old_repository_path=str(old_path),
        new_repository_path=str(new_path),
    )

    assert result["spec"]["claims"][0]["resource"] == new_resource_id
    assert result["spec"]["metadata"]["target_resource"] == new_resource_id


def test_preview_rejects_non_target_scoped_old_resource_id_when_target_extends_old(
    tmp_path: Path,
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    new_resource_id = f"{OLD_RESOURCE}:archive"
    spec["metadata"]["other_resource"] = f"{OLD_RESOURCE}:other"

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=new_resource_id,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


def test_preview_rejects_colon_continuation_of_target_resource_id(
    tmp_path: Path,
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    new_resource_id = f"{OLD_RESOURCE}:archive"
    spec["metadata"]["other_resource"] = f"{new_resource_id}:other"

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=new_resource_id,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


@pytest.mark.parametrize("delimiter", ("?", "#", "&"))
def test_preview_rejects_old_repository_path_after_uri_delimiter(
    tmp_path: Path, delimiter: str
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["execution"]["argv"] = ["tool", f"tool://open{delimiter}{old_path}"]

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


@pytest.mark.parametrize(
    "encoded_value",
    (
        "file://{old}%2Fscript",
        "file://%2F{old_without_slash}%2Fscript",
    ),
)
def test_preview_rejects_percent_encoded_old_repository_path(
    tmp_path: Path, encoded_value: str
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    old_text = str(old_path)
    spec["execution"]["argv"] = [
        "tool",
        encoded_value.format(
            old=old_text,
            old_without_slash=old_text.lstrip("/"),
        ),
    ]

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=old_text,
            new_repository_path=str(new_path),
        )


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    ((";", "/script"), ("", "|next"), ("<", ">"), ("`", "`")),
)
def test_preview_rejects_shell_operator_adjacent_old_repository_path(
    tmp_path: Path, prefix: str, suffix: str
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["execution"]["argv"] = ["sh", "-c", f"{prefix}{old_path}{suffix}"]

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


@pytest.mark.parametrize(
    "operator",
    (":-", "-", ":=", "=", ":?", "?", ":+", "+"),
)
def test_preview_rejects_old_repository_path_in_shell_parameter_expansion(
    tmp_path: Path, operator: str
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["execution"]["argv"] = [
        "sh",
        "-c",
        f'cd "${{WORKTREE{operator}{old_path}}}"',
    ]

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


@pytest.mark.parametrize("field", ("title", "goal"))
def test_preview_preserves_repository_path_mention_in_task_prose(
    tmp_path: Path, field: str
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    prose = f"Migrate {old_path} to the new repository identity"
    spec[field] = prose

    preview = task_specs.preview_repository_identity_rebind(
        spec,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
        old_repository_path=str(old_path),
        new_repository_path=str(new_path),
    )

    assert preview["spec"][field] == prose


def test_preview_preserves_resource_id_mention_in_acceptance_text(tmp_path: Path) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["acceptance"][0]["assertion"] = (
        f"Historical {OLD_RESOURCE} ancestry remains evidence, not execution authority"
    )
    acceptance_before = json.loads(canonical_json(spec["acceptance"]))

    preview = task_specs.preview_repository_identity_rebind(
        spec,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
        old_repository_path=str(old_path),
        new_repository_path=str(new_path),
    )

    assert preview["spec"]["acceptance"] == acceptance_before


def test_preview_preserves_repository_path_mention_in_acceptance_text(tmp_path: Path) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["acceptance"][0]["assertion"] = (
        f"Evidence was historically collected from {old_path}"
    )
    acceptance_before = json.loads(canonical_json(spec["acceptance"]))

    preview = task_specs.preview_repository_identity_rebind(
        spec,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
        old_repository_path=str(old_path),
        new_repository_path=str(new_path),
    )

    assert preview["spec"]["acceptance"] == acceptance_before


def test_preview_rejects_colon_scoped_old_repository_path_in_metadata(
    tmp_path: Path,
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["metadata"]["blocking_resource"] = f"repo:{old_path}:operation:cleanup"

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


def test_preview_rejects_embedded_old_resource_id_in_metadata(tmp_path: Path) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["metadata"]["resource_binding"] = f"lease={OLD_RESOURCE}"

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


def test_preview_rejects_old_repository_path_in_unapproved_metadata(tmp_path: Path) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["metadata"]["unexpected_old_path"] = str(old_path)

    with pytest.raises(task_specs.TaskSpecError, match="left old technical bindings"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


def test_regular_put_cannot_preempt_repository_rebind_idempotency_namespace(
    tmp_path: Path,
) -> None:
    root, _, _ = _registry_root(tmp_path, task_ids=("TASK-A",), legacy=False)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    current = store.task_spec("TASK-A")
    assert current is not None
    changed = json.loads(canonical_json(current["spec"]))
    changed["title"] = "ordinary writer must not reserve rebind identity"

    with pytest.raises(StateError, match="idempotency namespace is reserved"):
        store.put_task_spec(
            changed,
            idempotency_key="repository-identity-rebind:forged",
            expected_revision=current["revision"],
            source="ordinary-writer",
        )

    assert store.task_spec("TASK-A")["revision"] == 1


def test_preview_rejects_unscoped_old_path_inside_grabowski_resource(
    tmp_path: Path,
) -> None:
    _, old_path, new_path = _registry_root(
        tmp_path, task_ids=("TASK-A",), legacy=True
    )
    spec = _task("TASK-A", str(old_path), legacy=True)
    spec["execution"]["grabowski_resources"].append(f"path:/prefix{old_path}")

    with pytest.raises(task_specs.TaskSpecError, match="unscoped old repository path"):
        task_specs.preview_repository_identity_rebind(
            spec,
            old_resource_id=OLD_RESOURCE,
            new_resource_id=NEW_RESOURCE,
            old_repository_path=str(old_path),
            new_repository_path=str(new_path),
        )


def test_unrelated_taskspec_revision_invalidates_full_projection_plan(tmp_path: Path) -> None:
    root, _, new_path = _registry_root(tmp_path, legacy=False)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )

    unrelated = _task("UNRELATED-TASK", str(new_path), legacy=False)
    unrelated["claims"][0]["resource"] = NEW_RESOURCE
    unrelated["execution"]["grabowski_resources"] = [f"repo:{new_path}"]
    created = store.put_task_spec(
        unrelated,
        idempotency_key="unrelated-after-plan",
        expected_revision=None,
        source="test",
    )
    assert created["revision"] == 1

    with pytest.raises(StateError, match="TaskSpec projection root changed"):
        apply_plan(
            registry,
            store,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
        )

    assert store.task_spec("TASK-A")["revision"] == 1
    assert store.task_spec("TASK-B")["revision"] == 1


def test_finished_excluded_run_makes_applied_plan_stale(tmp_path: Path) -> None:
    root, _, _ = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    _insert_active_run(store, "TASK-B", "RUN-B")
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
        excluded_active_task_ids=("TASK-B",),
    )

    first = apply_plan(
        registry,
        store,
        plan,
        expected_plan_sha256=plan["plan_sha256"],
    )
    assert first["status"] == "applied"
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET state='succeeded',updated_at=?,heartbeat_at=? WHERE run_id=?",
            (utc_now(), utc_now(), "RUN-B"),
        )

    with pytest.raises(StateError, match="excluded task TASK-B is no longer active"):
        apply_plan(
            registry,
            store,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
        )

    fresh = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )
    assert fresh["summary"]["migration_items"] == 1
    assert fresh["excluded_active_tasks"] == []
    assert fresh["items"][0]["task_id"] == "TASK-B"


def test_rehashed_inconsistent_plan_summary_is_rejected(tmp_path: Path) -> None:
    root, _, _ = _registry_root(tmp_path, legacy=True)
    registry = Registry.load(root)
    store = _store(tmp_path, registry)
    plan = build_plan(
        registry,
        store,
        old_resource_id=OLD_RESOURCE,
        new_resource_id=NEW_RESOURCE,
    )
    forged = json.loads(canonical_json(plan))
    forged["summary"]["migration_items"] = 999
    forged["plan_sha256"] = plan_sha256(forged)

    with pytest.raises(StateError, match="plan summary is inconsistent"):
        apply_plan(
            registry,
            store,
            forged,
            expected_plan_sha256=forged["plan_sha256"],
        )

    assert store.task_spec("TASK-A")["revision"] == 1
    assert store.task_spec("TASK-B")["revision"] == 1
