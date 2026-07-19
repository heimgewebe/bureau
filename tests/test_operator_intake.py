from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import operator_intake as operator_intake_module
from bureau import runtime_identity as runtime_identity_module
from bureau.core import Registry, StateStore
from bureau.lease_contract import (
    BUREAU_REGISTRY_PUBLICATION_GATE_KEY,
    BUREAU_REPOSITORY_ROOT,
)
from bureau.live_register import live_register_record
from bureau.operator_intake import (
    OperatorIntakeError,
    SubprocessTaskPublisher,
    candidate_assess,
    candidate_record,
    candidate_record_request,
    publication_preview,
    publish_task_proposal,
    task_propose,
)
from bureau.registry_snapshot import snapshot_tree_sha256


def _git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return process.stdout.strip()


def _committed_registry(registry_factory) -> tuple[Path, Registry]:
    root = registry_factory(task_count=2)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root, Registry.load(root)



def _runtime_snapshot_registry(
    source: Path,
    tmp_path: Path,
    monkeypatch,
) -> tuple[Registry, Path]:
    snapshot = tmp_path / "runtime-snapshot"
    paths: list[Path] = []
    for candidate in sorted(source.rglob("*")):
        relative = candidate.relative_to(source)
        if ".git" in relative.parts or not candidate.is_file():
            continue
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, destination)
        paths.append(relative)
    tree_sha256 = snapshot_tree_sha256(snapshot, paths)
    assert tree_sha256 is not None
    source_commit = _git(source, "rev-parse", "HEAD")
    inventory = snapshot / ".bureau-runtime-snapshot.json"
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bureau_registry_snapshot",
                "source_commit": source_commit,
                "tree_sha256": tree_sha256,
                "paths": [path.as_posix() for path in paths],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    inventory_sha256 = hashlib.sha256(inventory.read_bytes()).hexdigest()

    module_path = Path(runtime_identity_module.__file__).resolve()
    release_root = module_path.parents[2]
    module_sha256 = runtime_identity_module._sha256(module_path)
    package_tree_sha256 = runtime_identity_module._package_tree_sha256(release_root)
    assert module_sha256 is not None
    assert package_tree_sha256 is not None
    manifest = tmp_path / "deployment-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bureau_runtime_deployment",
                "immutable_release_path": str(release_root),
                "module_path": str(module_path),
                "module_sha256": module_sha256,
                "package_tree_sha256": package_tree_sha256,
                "source_commit": source_commit,
                "release_id": f"{source_commit[:12]}-test",
                "canonical_registry_root": str(snapshot),
                "canonical_registry_inventory_path": str(inventory),
                "canonical_registry_inventory_sha256": inventory_sha256,
                "canonical_registry_tree_sha256": tree_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    monkeypatch.setenv("BUREAU_RUNTIME_MANIFEST", str(manifest))
    return Registry.load(snapshot), snapshot

def _record(registry: Registry, store: StateStore, *, key: str = "source:alpha"):
    return candidate_record(
        registry,
        store,
        idempotency_key=key,
        title="Create exact operator intake task",
        source_kind="conversation",
        source_locator="chat:alpha",
        source_sha256="a" * 64,
        desired_outcome="Create a typed and reviewed Bureau task publication path",
        repo="repo.alpha",
    )


def _task(root: Path, task_id: str = "BUR-TEST-001-T099") -> dict:
    return {
        "schema_version": 1,
        "id": task_id,
        "initiative": "BUR-TEST-001",
        "title": "Implement typed candidate publication",
        "state": "planned",
        "goal": "Publish one source-bound Bureau task through a reviewed plan.",
        "priority": {"lane": "later", "rank": 99},
        "execution": {
            "mode": "interactive-agent",
            "policy": "review-before-effect",
            "working_repository": str(root),
            "approval": {
                "action_class": "repository_mutation",
                "required_level": "operator",
            },
        },
        "claims": [{"resource": "repo.alpha", "mode": "write", "isolation": "worktree"}],
        "required_capabilities": ["repository", "shell", "bureau"],
        "depends_on": ["BUR-TEST-001-T001"],
        "acceptance": [
            {
                "id": "typed-result",
                "assertion": "The exact candidate is published with a typed receipt.",
                "verifier": "tests",
                "evidence_type": "object",
            }
        ],
    }


def _review(plan_path: Path, *, unresolved: list[str] | None = None) -> dict:
    plan = json.loads(plan_path.read_text())
    if unresolved is not None:
        plan["unresolved_fields"] = unresolved
        unsigned = {
            key: value for key, value in plan.items() if key not in {"proposal_sha256", "review"}
        }
        from bureau.legacy import sha256_json

        plan["proposal_sha256"] = sha256_json(unsigned)
    plan["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "operator-self-review",
        "reviewed_at": "2026-07-18T08:00:00+02:00",
        "reviewed_proposal_sha256": plan["proposal_sha256"],
    }
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    return plan


def _proposal(registry: Registry, store: StateStore, tmp_path: Path) -> Path:
    recorded = _record(registry, store)
    path = tmp_path / "proposal.json"
    task_propose(
        registry,
        store,
        candidate_id=recorded["candidate_id"],
        task_json=_task(registry.root),
        publishing_task_id="BUR-TEST-001-T001",
        path=path,
    )
    return path


def _lease_binding(*, owner: str = "operator-test", task_id: str = "BUR-TEST-001-T001") -> dict:
    return {"owner_id": owner, "task_id": task_id}


def _lease_db(
    preview: dict,
    tmp_path: Path,
    *,
    owner: str = "operator-test",
    gate_ttl: int = 240,
    omit: set[str] | None = None,
    metadata_overrides: dict[str, object] | None = None,
    metadata_digest: str | None = None,
) -> Path:
    path = tmp_path / "grabowski-resources.sqlite3"
    path.unlink(missing_ok=True)
    acquired = int(time.time())
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE leases ("
        "resource_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
        "purpose TEXT NOT NULL, acquired_at_unix INTEGER NOT NULL, "
        "updated_at_unix INTEGER NOT NULL, expires_at_unix INTEGER NOT NULL, "
        "metadata_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL, "
        "reclaimed_from_owner TEXT)"
    )
    connection.execute("INSERT INTO metadata(key, value) VALUES('schema_version', '2')")
    lease_metadata: dict[str, object] = {
        "task_id": "BUR-TEST-001-T001",
        "operation": "registry-publication",
        "proposal_sha256": preview["proposal_sha256"],
    }
    if metadata_overrides:
        lease_metadata.update(metadata_overrides)
    metadata_json = json.dumps(
        lease_metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = metadata_digest or hashlib.sha256(metadata_json.encode("utf-8")).hexdigest()
    omitted = omit or set()
    for key in preview["required_resource_keys"]:
        if key in omitted:
            continue
        ttl = gate_ttl if key == BUREAU_REGISTRY_PUBLICATION_GATE_KEY else 1800
        connection.execute(
            "INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                key,
                owner,
                "operator-intake-test",
                acquired,
                acquired,
                acquired + ttl,
                digest,
                metadata_json,
            ),
        )
    connection.commit()
    connection.close()
    path.chmod(0o600)
    return path


class FakePublisher:
    def __init__(
        self,
        *,
        mutate_plan: Path | None = None,
        fail: Exception | None = None,
        fail_phase: str = "push_attempted",
    ):
        self.calls = 0
        self.mutate_plan = mutate_plan
        self.fail = fail
        self.fail_phase = fail_phase

    def publish(
        self,
        *,
        registry,
        plan,
        workspace_root,
        assert_plan_unchanged,
        phase_changed,
    ):
        self.calls += 1
        assert_plan_unchanged()
        phase_changed("local_workspace")
        if self.mutate_plan is not None:
            self.mutate_plan.write_text(self.mutate_plan.read_text() + " ")
        if self.fail is not None:
            phase_changed(self.fail_phase)
            raise self.fail
        assert_plan_unchanged()
        phase_changed("committed_locally")
        phase_changed("push_attempted")
        phase_changed("push_confirmed")
        phase_changed("pr_attempted")
        phase_changed("pr_confirmed")
        return {
            "repository": "example/bureau",
            "workspace": str(workspace_root / "fixture"),
            "branch": "operator/register-fixture",
            "head": "f" * 40,
            "pull_request": {"number": 7, "state": "OPEN"},
            "url": "https://example.invalid/pull/7",
            "git_diff_sha256": "b" * 64,
            "target_file_sha256": plan["task_file_sha256"],
            "readback_complete": True,
        }


def test_candidate_record_is_idempotent_and_source_bound(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    second = _record(registry, store)
    assert first["status"] == "recorded"
    assert second["status"] == "existing"
    assert second["event_id"] == first["event_id"]
    assert second["idempotent_replay"] is True
    context = first["record"]["operator_intake"]
    assert context["source"]["sha256"] == "a" * 64
    assert context["source"]["freshness"] == "digest-bound"


def test_candidate_record_is_idempotent_under_parallel_first_write(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    worker_count = 16
    first_read_barrier = threading.Barrier(worker_count)
    thread_state = threading.local()
    original_candidate_records = operator_intake_module.candidate_records

    def coordinated_candidate_records(observed_store):
        if not getattr(thread_state, "first_read_complete", False):
            thread_state.first_read_complete = True
            first_read_barrier.wait(timeout=10)
            return []
        return original_candidate_records(observed_store)

    monkeypatch.setattr(operator_intake_module, "candidate_records", coordinated_candidate_records)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(
            executor.map(
                lambda _: _record(registry, store, key="source:parallel"),
                range(worker_count),
            )
        )

    assert [result["status"] for result in results].count("recorded") == 1
    assert [result["status"] for result in results].count("existing") == worker_count - 1
    assert len({result["candidate_id"] for result in results}) == 1
    assert len({result["event_id"] for result in results}) == 1
    assert all(result["ambiguity"] is False for result in results)
    assert len(original_candidate_records(store)) == 1


def test_candidate_record_parallel_conflicting_request_fails_closed(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first_read_barrier = threading.Barrier(2)
    thread_state = threading.local()
    original_candidate_records = operator_intake_module.candidate_records

    def coordinated_candidate_records(observed_store):
        if not getattr(thread_state, "first_read_complete", False):
            thread_state.first_read_complete = True
            first_read_barrier.wait(timeout=10)
            return []
        return original_candidate_records(observed_store)

    def record(title):
        try:
            result = candidate_record(
                registry,
                store,
                idempotency_key="source:parallel-conflict",
                title=title,
                source_kind="conversation",
                source_locator="chat:parallel-conflict",
                source_sha256="c" * 64,
                desired_outcome="Prove conflicting parallel input fails closed",
                repo="repo.alpha",
            )
        except OperatorIntakeError as exc:
            return {"status": "failed", "code": exc.code}
        return {"status": result["status"], "code": None}

    monkeypatch.setattr(operator_intake_module, "candidate_records", coordinated_candidate_records)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(record, ["First request", "Second request"]))

    assert [result["status"] for result in results].count("recorded") == 1
    assert [result["code"] for result in results].count("idempotency-conflict") == 1
    assert len(original_candidate_records(store)) == 1


def test_candidate_replay_returns_current_superseding_event_without_self_duplicate(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    correction = live_register_record(
        registry,
        store,
        kind="candidate_task",
        title="Corrected operator intake task",
        source="operator-intake-correction",
        repo="repo.alpha",
        candidate_id=first["candidate_id"],
        status="active",
        promotion_required=True,
        supersedes_event_id=first["event_id"],
        note="Corrected wording without creating a new candidate identity",
    )
    replay = _record(registry, store)
    assert replay["candidate_id"] == first["candidate_id"]
    assert replay["event_id"] == correction["event_id"]
    result = candidate_assess(registry, store, candidate_id=first["candidate_id"])
    assert result["exact_duplicates"] == []
    assert not any(
        item.get("id") == first["candidate_id"] for item in result["similarity_suggestions"]
    )


def test_candidate_request_rejects_unknown_fields(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    with pytest.raises(OperatorIntakeError) as caught:
        candidate_record_request(
            registry,
            store,
            {
                "schema_version": 1,
                "idempotency_key": "typed:unknown",
                "title": "Unknown field",
                "source_kind": "fixture",
                "desired_outcome": "Reject transport drift",
                "repo": "repo.alpha",
                "invented_authority": True,
            },
        )
    assert caught.value.code == "request-fields-unknown"
    assert caught.value.details == {"unknown_fields": ["invented_authority"]}


def test_candidate_record_rejects_idempotency_conflict(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    _record(registry, store)
    with pytest.raises(OperatorIntakeError, match="different candidate input") as caught:
        candidate_record(
            registry,
            store,
            idempotency_key="source:alpha",
            title="Different",
            source_kind="conversation",
            source_locator="chat:alpha",
            desired_outcome="Different outcome",
            repo="repo.alpha",
        )
    assert caught.value.code == "idempotency-conflict"
    assert caught.value.effect_started is False



def test_candidate_assess_accepts_manifest_bound_runtime_snapshot(
    registry_factory, tmp_path, monkeypatch
):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(registry, store)
    snapshot_registry, _ = _runtime_snapshot_registry(root, tmp_path, monkeypatch)

    result = candidate_assess(
        snapshot_registry,
        store,
        candidate_id=recorded["candidate_id"],
        initiative="BUR-TEST-001",
        task_id="BUR-TEST-001-T099",
    )

    assert result["decision"] == "promote"
    assert result["advisory_only"] is True


def test_candidate_assess_rejects_runtime_snapshot_with_invalid_manifest(
    registry_factory, tmp_path, monkeypatch
):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(registry, store)
    snapshot_registry, _ = _runtime_snapshot_registry(root, tmp_path, monkeypatch)
    manifest = tmp_path / "deployment-manifest.json"
    payload = json.loads(manifest.read_text())
    payload["module_sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(OperatorIntakeError) as caught:
        candidate_assess(
            snapshot_registry,
            store,
            candidate_id=recorded["candidate_id"],
        )

    assert caught.value.code == "registry-git-read-failed"



def test_candidate_assess_rejects_runtime_snapshot_drift_during_reload(
    registry_factory, tmp_path, monkeypatch
):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(registry, store)
    snapshot_registry, snapshot = _runtime_snapshot_registry(root, tmp_path, monkeypatch)
    original_load = Registry.load
    target = snapshot / "registry/tasks/BUR-TEST-001-T001.json"

    def drifting_load(candidate_root):
        loaded = original_load(candidate_root)
        if Path(candidate_root).resolve() == snapshot.resolve():
            payload = json.loads(target.read_text())
            payload["title"] = "Tampered during reload"
            target.write_text(json.dumps(payload, indent=2) + "\n")
        return loaded

    monkeypatch.setattr(operator_intake_module.Registry, "load", staticmethod(drifting_load))

    with pytest.raises(OperatorIntakeError) as caught:
        candidate_assess(
            snapshot_registry,
            store,
            candidate_id=recorded["candidate_id"],
        )

    assert caught.value.code == "registry-snapshot-drift"
    assert caught.value.retryable is True

def test_candidate_assessment_is_advisory_and_promotes_complete_input(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(registry, store)
    result = candidate_assess(
        registry,
        store,
        candidate_id=recorded["candidate_id"],
        initiative="BUR-TEST-001",
        task_id="BUR-TEST-001-T099",
    )
    assert result["decision"] == "promote"
    assert result["advisory_only"] is True
    assert result["exact_duplicates"] == []
    assert result["target"]["publication_approval"]["allowed"] is False


def test_candidate_assessment_reports_exact_source_duplicate(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    second = candidate_record(
        registry,
        store,
        idempotency_key="source:beta",
        title="Another view",
        source_kind="conversation",
        source_locator="chat:beta",
        source_sha256="a" * 64,
        desired_outcome="Same source observed again",
        repo="repo.alpha",
    )
    result = candidate_assess(registry, store, candidate_id=second["candidate_id"])
    assert result["decision"] == "merge"
    assert result["exact_duplicates"] == [
        {
            "kind": "candidate-source-digest",
            "candidate_id": first["candidate_id"],
            "event_id": first["event_id"],
            "reason": "same source_sha256",
        }
    ]


def test_task_proposal_binds_candidate_registry_and_review(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = json.loads(plan_path.read_text())
    assert plan["candidate"]["candidate_id"].startswith("candidate-")
    assert plan["registry"]["commit"] == _git(registry.root, "rev-parse", "HEAD")
    assert plan["task_json"]["metadata"]["operator_intake"]["event_id"] == 1
    assert plan["review"]["status"] == "pending"
    assert plan["publication"] == {
        "action_class": "registry_mutation",
        "required_level": "reviewed_plan",
        "queue_mutated": False,
    }


def test_canonical_registry_snapshot_rejects_head_drift(registry_factory, monkeypatch):
    _, registry = _committed_registry(registry_factory)
    real_git_value = operator_intake_module._git_value
    head_reads = 0

    def drifting_git_value(root: Path, *arguments: str) -> str:
        nonlocal head_reads
        value = real_git_value(root, *arguments)
        if arguments == ("rev-parse", "HEAD"):
            head_reads += 1
            if head_reads == 2:
                return "f" * 40
        return value

    monkeypatch.setattr(operator_intake_module, "_git_value", drifting_git_value)

    with pytest.raises(OperatorIntakeError) as caught:
        operator_intake_module._canonical_registry_snapshot(registry)

    assert caught.value.code == "registry-snapshot-drift"
    assert caught.value.retryable is True


def test_candidate_record_rejects_dirty_registry_worktree(registry_factory, tmp_path):
    root, _ = _committed_registry(registry_factory)
    task_path = root / "registry" / "tasks" / "BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text())
    task["title"] = "Uncommitted Registry title"
    task_path.write_text(json.dumps(task, indent=2) + "\n")
    dirty_registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")

    with pytest.raises(OperatorIntakeError) as caught:
        candidate_record(
            dirty_registry,
            store,
            idempotency_key="source:dirty-registry",
            title="Dirty Registry candidate",
            source_kind="conversation",
            source_locator="chat:dirty-registry",
            source_sha256="c" * 64,
            desired_outcome="Reject uncommitted Registry truth",
            repo="repo.alpha",
        )

    assert caught.value.code == "registry-working-tree-dirty"


def test_candidate_assess_rejects_dirty_registry_schema(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(registry, store)
    schema_path = root / "schemas" / "task.v1.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["title"] = "Uncommitted task schema"
    schema_path.write_text(json.dumps(schema, indent=2) + "\n")
    dirty_registry = Registry.load(root)

    with pytest.raises(OperatorIntakeError) as caught:
        candidate_assess(
            dirty_registry,
            store,
            candidate_id=recorded["candidate_id"],
        )

    assert caught.value.code == "registry-working-tree-dirty"


def test_task_proposal_rejects_dirty_registry_worktree(registry_factory, tmp_path):
    root, clean_registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(clean_registry, store)
    task_path = root / "registry" / "tasks" / "BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text())
    task["title"] = "Uncommitted Registry title"
    task_path.write_text(json.dumps(task, indent=2) + "\n")
    dirty_registry = Registry.load(root)
    plan_path = tmp_path / "proposal.json"

    with pytest.raises(OperatorIntakeError) as caught:
        task_propose(
            dirty_registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=_task(root),
            publishing_task_id="BUR-TEST-001-T001",
            path=plan_path,
        )

    assert caught.value.code == "registry-working-tree-dirty"
    assert not plan_path.exists()


def test_candidate_assess_reloads_registry_after_stale_object(registry_factory, tmp_path):
    root, clean_registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = candidate_record(
        clean_registry,
        store,
        idempotency_key="source:stale-registry",
        title="Uncommitted Registry title",
        source_kind="conversation",
        source_locator="chat:stale-registry",
        source_sha256="b" * 64,
        desired_outcome="Uncommitted Registry title",
        repo="repo.alpha",
    )
    task_path = root / "registry" / "tasks" / "BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text())
    task["title"] = "Uncommitted Registry title"
    task_path.write_text(json.dumps(task, indent=2) + "\n")
    stale_registry = Registry.load(root)
    _git(root, "checkout", "--", "registry/tasks/BUR-TEST-001-T001.json")

    result = candidate_assess(
        stale_registry,
        store,
        candidate_id=recorded["candidate_id"],
    )

    assert all(
        item.get("title") != "Uncommitted Registry title"
        for item in result["similarity_suggestions"]
    )
    assert _git(root, "status", "--porcelain=v1", "--", "registry") == ""


def test_task_proposal_rejects_generic_acceptance_without_justification(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(registry, store)
    task = _task(registry.root)
    task["acceptance"] = [{"id": "source-event-bound", "assertion": "generic source"}]
    with pytest.raises(OperatorIntakeError) as caught:
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=task,
            publishing_task_id="BUR-TEST-001-T001",
            path=tmp_path / "proposal.json",
        )
    assert caught.value.code == "generic-placeholder-rejected"


def test_publication_preview_rejects_dirty_registry_worktree(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    task_path = root / "registry" / "tasks" / "BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text())
    task["title"] = "Uncommitted Registry title"
    task_path.write_text(json.dumps(task, indent=2) + "\n")
    dirty_registry = Registry.load(root)

    with pytest.raises(OperatorIntakeError) as caught:
        publication_preview(dirty_registry, store, plan_path=plan_path)

    assert caught.value.code == "registry-working-tree-dirty"


def test_publication_preview_requires_review_and_returns_exact_leases(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    with pytest.raises(OperatorIntakeError) as caught:
        publication_preview(registry, store, plan_path=plan_path)
    assert caught.value.code == "review-missing"
    plan = _review(plan_path)
    result = publication_preview(registry, store, plan_path=plan_path)
    assert result["status"] == "ready"
    assert result["approval"]["allowed"] is True
    assert result["required_resource_keys"] == sorted(
        [
            f"path:{BUREAU_REPOSITORY_ROOT / plan['target_path']}",
            BUREAU_REGISTRY_PUBLICATION_GATE_KEY,
        ]
    )


def test_publication_preview_rejects_unresolved_fields(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path, unresolved=["acceptance.live-proof"])
    with pytest.raises(OperatorIntakeError) as caught:
        publication_preview(registry, store, plan_path=plan_path)
    assert caught.value.code == "proposal-unresolved"


def test_publication_rejects_missing_lease_before_publisher(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    publisher = FakePublisher()
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path, omit=set(preview["required_resource_keys"])),
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    assert caught.value.code == "lease-resources-missing"
    assert publisher.calls == 0


def test_publication_rejects_lease_metadata_binding_mismatch(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(
                preview,
                tmp_path,
                metadata_overrides={"proposal_sha256": "0" * 64},
            ),
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=FakePublisher(),
        )
    assert caught.value.code == "lease-metadata-binding-mismatch"
    assert caught.value.details["mismatched"]["proposal_sha256"] == {
        "expected": preview["proposal_sha256"],
        "observed": "0" * 64,
    }


def test_publication_rejects_overlong_effect_gate_lease(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path, gate_ttl=301),
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=FakePublisher(),
        )
    assert caught.value.code == "publication-gate-ttl-invalid"


def test_publication_writes_receipt_and_is_idempotent(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    publisher = FakePublisher()
    receipt = tmp_path / "receipt.json"
    first = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
        publisher=publisher,
    )
    second = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
        publisher=publisher,
    )
    assert first["status"] == "published"
    assert first["queue_mutated"] is False
    assert second["idempotent_replay"] is True
    assert publisher.calls == 1


def test_publication_receipt_replay_survives_later_registry_drift(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    receipt = tmp_path / "receipt.json"
    publisher = FakePublisher()
    first = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
        publisher=publisher,
    )
    (root / "README.md").write_text("later registry-adjacent commit\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "later change")
    drifted = Registry.load(root)
    replay = publish_task_proposal(
        drifted,
        store,
        plan_path=plan_path,
        lease_binding={"owner_id": "expired", "task_id": "wrong"},
        resource_db=tmp_path / "missing-after-receipt.sqlite3",
        workspace_root=tmp_path / "unused",
        receipt_path=receipt,
        publisher=FakePublisher(fail=AssertionError("must not execute")),
    )
    assert replay["receipt_sha256"] == first["receipt_sha256"]
    assert replay["idempotent_replay"] is True
    assert publisher.calls == 1


def test_publication_rejects_tampered_existing_receipt(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    receipt = tmp_path / "receipt.json"
    publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
        publisher=FakePublisher(),
    )
    tampered = json.loads(receipt.read_text())
    tampered["task_id"] = "BUR-TEST-001-T777"
    receipt.write_text(json.dumps(tampered))
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "unused",
            receipt_path=receipt,
            publisher=FakePublisher(),
        )
    assert caught.value.code == "receipt-integrity-invalid"


def test_publication_detects_plan_byte_drift(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=FakePublisher(mutate_plan=plan_path),
        )
    assert caught.value.code == "plan-file-drift"
    assert not (tmp_path / "receipt.json").exists()


def test_publishing_task_revision_drift_fails_before_workspace_or_effects(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    publishing_task_path = root / "registry" / "tasks" / "BUR-TEST-001-T001.json"
    publishing_task = json.loads(publishing_task_path.read_text())
    publishing_task["title"] = "Changed publishing task revision"
    publishing_task_path.write_text(json.dumps(publishing_task, indent=2) + "\n")
    _git(root, "add", str(publishing_task_path.relative_to(root)))
    _git(root, "commit", "-m", "change publishing task")
    drifted_registry = Registry.load(root)
    resource_db = _lease_db(preview, tmp_path)
    publisher = FakePublisher()

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            drifted_registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    assert caught.value.code == "publishing-task-drift"
    assert caught.value.effect_started is False
    assert publisher.calls == 0
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "receipt.json").exists()
    assert _lease_count(resource_db) == len(preview["required_resource_keys"])


def test_publication_receipt_write_failure_is_ambiguous_only_for_receipt_and_retries_exactly(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    blocked_parent = tmp_path / "receipt-parent-is-file"
    blocked_parent.write_text("not a directory")
    receipt = blocked_parent / "receipt.json"
    publisher = FaultInjectingLocalPublisher(fail_after=("never",))
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "workspaces",
            receipt_path=receipt,
            publisher=publisher,
        )
    assert caught.value.code == "receipt-write-unclear"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert caught.value.details["publication_confirmed"] is True
    assert caught.value.details["ambiguity_scope"] == "receipt"
    assert caught.value.details["publication"]["readback_complete"] is True
    assert caught.value.required_readback == (f"publication receipt at {receipt}",)

    blocked_parent.unlink()
    blocked_parent.mkdir()
    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
        publisher=publisher,
    )

    assert result["status"] == "published"
    assert result["publication"]["readback_complete"] is True
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 1
    assert sum(command[:3] == ("gh", "pr", "create") for command in publisher.commands) == 1


def test_publication_wraps_unknown_publisher_failure_as_ambiguous(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=FakePublisher(fail=RuntimeError("transport disappeared")),
        )
    assert caught.value.code == "publication-unclear"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert "remote branch head" in caught.value.required_readback


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:heimgewebe/bureau.git", "heimgewebe/bureau"),
        ("ssh://git@github.com/heimgewebe/bureau.git", "heimgewebe/bureau"),
        ("https://github.com/heimgewebe/bureau", "heimgewebe/bureau"),
        ("https://github.com/heimgewebe/bureau.git/", "heimgewebe/bureau"),
    ],
)
def test_github_slug_accepts_only_canonical_github_remote_forms(remote, expected):
    assert SubprocessTaskPublisher._github_slug(remote) == expected


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.invalid/github.com/heimgewebe/bureau.git",
        "https://github.com.evil.invalid/heimgewebe/bureau.git",
        "http://github.com/heimgewebe/bureau.git",
        "https://github.com/heimgewebe/bureau/extra.git",
        "https://github.com/heimgewebe/bureau.git?token=secret",
        "git@github.com:../bureau.git",
        "github.com/heimgewebe/bureau.git",
    ],
)
def test_github_slug_rejects_noncanonical_or_ambiguous_remotes(remote):
    with pytest.raises(OperatorIntakeError) as caught:
        SubprocessTaskPublisher._github_slug(remote)
    assert caught.value.code == "github-remote-invalid"


class LocalGitPublisher(SubprocessTaskPublisher):
    def __init__(self):
        self.pull_request = None

    @staticmethod
    def _github_slug(remote: str) -> str:
        return "example/bureau"

    def _run(self, arguments, *, cwd=None, timeout=60):
        if list(arguments[:3]) == ["gh", "pr", "create"]:
            assert cwd is not None
            head = super()._run(["git", "rev-parse", "HEAD"], cwd=cwd)
            branch = super()._run(["git", "branch", "--show-current"], cwd=cwd)
            self.pull_request = {
                "number": 7,
                "url": "https://example.invalid/pull/7",
                "state": "OPEN",
                "headRefOid": head,
                "headRefName": branch,
                "baseRefName": "main",
            }
            return "https://example.invalid/pull/7"
        if list(arguments[:3]) == ["gh", "pr", "list"]:
            return json.dumps([] if self.pull_request is None else [self.pull_request])
        if list(arguments[:3]) == ["gh", "pr", "view"]:
            return json.dumps(self.pull_request)
        return super()._run(arguments, cwd=cwd, timeout=timeout)


def _publication_commit_count(commands: list[tuple[str, ...]]) -> int:
    return sum(
        "commit" in command or "commit-tree" in command
        for command in commands
    )


class FaultInjectingLocalPublisher(LocalGitPublisher):
    def __init__(self, *, fail_after: tuple[str, ...]):
        super().__init__()
        self.fail_after = fail_after
        self.injected = False
        self.commands: list[tuple[str, ...]] = []

    def _run(self, arguments, *, cwd=None, timeout=60):
        command = tuple(arguments)
        self.commands.append(command)
        value = super()._run(arguments, cwd=cwd, timeout=timeout)
        fault_matches = any(
            command[index : index + len(self.fail_after)] == self.fail_after
            for index in range(len(command) - len(self.fail_after) + 1)
        )
        if not self.injected and fault_matches:
            self.injected = True
            raise RuntimeError(f"fault after {' '.join(self.fail_after)}")
        return value


class PreEffectInterruptionPublisher(FaultInjectingLocalPublisher):
    def __init__(self, *, interruption: str):
        super().__init__(fail_after=("never",))
        self.interruption = interruption

    def _interrupt(self, point: str) -> None:
        if not self.injected and self.interruption == point:
            self.injected = True
            raise RuntimeError(f"interruption at {point}")

    def _before_workspace_rename(self, staging: Path, workspace: Path) -> None:
        self._interrupt("before_workspace_rename")

    def _before_clone(self, staging: Path) -> None:
        self._interrupt("before_clone_destination")

    def _after_clone_destination_created(self, staging: Path) -> None:
        self._interrupt("after_clone_destination")

    def _after_target_temp_created(self, temporary: Path) -> None:
        self._interrupt("target_temp_created")

    def _after_target_temp_fsync(self, temporary: Path) -> None:
        self._interrupt("target_temp_fsync")

    def _after_target_rename(self, target: Path) -> None:
        self._interrupt("target_renamed")


class TargetMutationPublisher(FaultInjectingLocalPublisher):
    def __init__(self, *, mutation_point: str):
        super().__init__(fail_after=("never",))
        self.mutation_point = mutation_point

    def _mutate(self, point: str, target: Path) -> None:
        if not self.injected and self.mutation_point == point:
            self.injected = True
            target.write_bytes(b'{"foreign":"changed bytes"}\n')

    def _before_git_add(self, target: Path) -> None:
        self._mutate("before_git_add", target)

    def _before_git_commit(self, target: Path) -> None:
        self._mutate("before_git_commit", target)


class ImmutableTreeRacePublisher(FaultInjectingLocalPublisher):
    def __init__(self):
        super().__init__(fail_after=("never",))

    def _before_publication_ref_update(self, workspace: Path, target: Path, commit: str) -> None:
        if self.injected:
            return
        self.injected = True
        target.write_bytes(b'{"foreign":"after tree capture"}\n')
        subprocess.run(
            ["git", "add", "--", str(target.relative_to(workspace))],
            cwd=workspace,
            check=True,
            capture_output=True,
        )


class MarkerlessStagingReplacementPublisher(PreEffectInterruptionPublisher):
    def __init__(self):
        super().__init__(interruption="after_clone_destination")
        self.replaced = False
        self.displaced: Path | None = None

    def _before_markerless_staging_remove(self, staging: Path) -> None:
        if self.replaced:
            return
        self.replaced = True
        self.displaced = staging.with_name(staging.name + ".displaced")
        staging.rename(self.displaced)
        staging.mkdir()
        (staging / "FOREIGN.txt").write_text("must remain\n")


class MarkerInterruptionPublisher(FaultInjectingLocalPublisher):
    def __init__(self, *, marker_phase: str, interruption: str):
        super().__init__(fail_after=("never",))
        self.marker_phase = marker_phase
        self.interruption = interruption

    def _interrupt_marker(self, point: str) -> None:
        if (
            not self.injected
            and self._publication_phase == self.marker_phase
            and self.interruption == point
        ):
            self.injected = True
            raise RuntimeError(
                f"marker interruption at {self.marker_phase}:{self.interruption}"
            )

    def _after_marker_temp_created(self, temporary: Path) -> None:
        self._interrupt_marker("temp_created")

    def _after_marker_temp_written(self, temporary: Path) -> None:
        self._interrupt_marker("temp_written")

    def _after_marker_temp_fsync(self, temporary: Path) -> None:
        self._interrupt_marker("temp_fsync")

    def _before_marker_replace(self, temporary: Path, marker: Path) -> None:
        self._interrupt_marker("before_replace")


def _local_remote(root: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(root, "remote", "add", "origin", str(remote))
    fixture_head = _git(root, "rev-parse", "HEAD")
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "config",
            "remote.origin.url",
            str(root),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "fetch",
            str(root),
            fixture_head,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(remote), "update-ref", "refs/heads/main", fixture_head],
        check=True,
        capture_output=True,
    )
    return remote


def _advance_remote_main(remote: Path, tmp_path: Path) -> None:
    clone = tmp_path / "remote-main-advance"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.name", "Remote Test")
    _git(clone, "config", "user.email", "remote@example.invalid")
    (clone / "REMOTE.txt").write_text("remote main advanced\n")
    _git(clone, "add", "REMOTE.txt")
    _git(clone, "commit", "-m", "advance remote main")
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "fetch",
            str(clone),
            "HEAD:refs/heads/main",
        ],
        check=True,
        capture_output=True,
    )


def _lease_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT count(*) FROM leases").fetchone()[0])
    finally:
        connection.close()


def test_subprocess_publisher_creates_only_target_branch_and_task_file(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
    remote = _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=tmp_path / "receipt.json",
        publisher=LocalGitPublisher(),
    )
    branch = result["branch"]
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    changed = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            remote_head,
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert changed == [plan["target_path"]]
    task_bytes = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "show",
            f"{remote_head}:{plan['target_path']}",
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert task_bytes.decode().endswith("\n")
    assert json.loads(task_bytes)["id"] == plan["task_id"]
    assert result["publication"]["readback_complete"] is True


@pytest.mark.parametrize(
    ("mutation_point", "expected_code"),
    [
        ("before_git_add", "publication-index-target-hash-mismatch"),
        ("before_git_commit", "publication-precommit-target-hash-mismatch"),
    ],
)
def test_worktree_byte_mutation_never_reaches_commit_or_remote(
    registry_factory, tmp_path, mutation_point, expected_code
):
    root, registry = _committed_registry(registry_factory)
    remote = _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = TargetMutationPublisher(mutation_point=mutation_point)

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    workspace = workspace_root / plan["proposal_sha256"][:20]
    assert caught.value.code == expected_code
    assert caught.value.publication_phase == "local_workspace"
    assert _git(workspace, "rev-parse", "HEAD") == plan["registry"]["commit"]
    assert _git(workspace, "rev-list", "--count", f"{plan['registry']['commit']}..HEAD") == "0"
    remote_branch = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "rev-parse",
            "--verify",
            f"refs/heads/{_publication_branch_for_test(plan)}",
        ],
        capture_output=True,
        check=False,
    )
    assert remote_branch.returncode != 0
    assert _publication_commit_count(publisher.commands) == 0
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 0
    assert publisher.pull_request is None


def test_mutation_after_tree_capture_cannot_change_commit_or_reach_remote(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    remote = _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = ImmutableTreeRacePublisher()
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    workspace = workspace_root / plan["proposal_sha256"][:20]
    head = _git(workspace, "rev-parse", "HEAD")
    committed = subprocess.run(
        ["git", "-C", str(workspace), "show", f"{head}:{plan['target_path']}"],
        capture_output=True,
        check=True,
    ).stdout
    assert caught.value.code == "publication-postcommit-workspace-drift"
    assert hashlib.sha256(committed).hexdigest() == plan["task_file_sha256"]
    assert _git(workspace, "rev-list", "--count", f"{plan['registry']['commit']}..HEAD") == "1"
    remote_branch = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "rev-parse",
            "--verify",
            f"refs/heads/{_publication_branch_for_test(plan)}",
        ],
        capture_output=True,
        check=False,
    )
    assert remote_branch.returncode != 0
    assert publisher.pull_request is None


def _publication_branch_for_test(plan: dict) -> str:
    return operator_intake_module._publication_branch(
        plan["task_id"], plan["proposal_sha256"]
    )


@pytest.mark.parametrize(
    "interruption", ["before_clone_destination", "after_clone_destination"]
)
def test_clone_destination_interruption_retries_from_exact_reservation_once(
    registry_factory, tmp_path, interruption
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / plan["proposal_sha256"][:20]
    staging = workspace.with_name(
        f".{workspace.name}{SubprocessTaskPublisher._STAGING_SUFFIX}"
    )
    reservation = SubprocessTaskPublisher._reservation_path(workspace)
    publisher = PreEffectInterruptionPublisher(interruption=interruption)

    with pytest.raises(OperatorIntakeError):
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    reservation_value = json.loads(reservation.read_text())
    assert reservation.is_file()
    assert reservation_value["proposal_sha256"] == plan["proposal_sha256"]
    assert reservation_value["base_commit"] == plan["registry"]["commit"]
    assert reservation_value["branch"] == _publication_branch_for_test(plan)
    assert reservation_value["staging_path"] == str(staging)
    assert reservation_value["final_path"] == str(workspace)
    assert staging.exists() is (interruption == "after_clone_destination")

    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=workspace_root,
        receipt_path=tmp_path / "receipt.json",
        publisher=publisher,
    )

    assert result["publication"]["workspace_reconciled"] is True
    assert not reservation.exists()
    assert not staging.exists()
    assert _git(workspace, "rev-list", "--count", f"{plan['registry']['commit']}..HEAD") == "1"
    assert sum(command[:2] == ("git", "clone") for command in publisher.commands) == 1
    assert _publication_commit_count(publisher.commands) == 1
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 1
    assert sum(command[:3] == ("gh", "pr", "create") for command in publisher.commands) == 1


@pytest.mark.parametrize("reservation_state", ["missing", "malformed", "foreign"])
def test_markerless_staging_without_exact_reservation_blocks_without_deletion(
    registry_factory, tmp_path, reservation_state
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / plan["proposal_sha256"][:20]
    staging = workspace.with_name(
        f".{workspace.name}{SubprocessTaskPublisher._STAGING_SUFFIX}"
    )
    reservation = SubprocessTaskPublisher._reservation_path(workspace)
    publisher = PreEffectInterruptionPublisher(interruption="after_clone_destination")
    with pytest.raises(OperatorIntakeError):
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    staging_inode = staging.stat().st_ino
    if reservation_state == "missing":
        reservation.unlink()
    elif reservation_state == "malformed":
        reservation.write_text("{not-json\n")
    else:
        value = json.loads(reservation.read_text())
        value["proposal_sha256"] = "0" * 64
        unsigned = {key: item for key, item in value.items() if key != "reservation_sha256"}
        value["reservation_sha256"] = operator_intake_module.legacy.sha256_json(unsigned)
        reservation.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        )

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    assert caught.value.code in {
        "workspace-reservation-invalid",
        "workspace-reservation-mismatch",
    }
    assert staging.is_dir()
    assert staging.stat().st_ino == staging_inode
    assert sum(command[:2] == ("git", "clone") for command in publisher.commands) == 0
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 0


def test_markerless_staging_replacement_race_blocks_without_deleting_foreign_path(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = MarkerlessStagingReplacementPublisher()
    with pytest.raises(OperatorIntakeError):
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    workspace = workspace_root / plan["proposal_sha256"][:20]
    staging = workspace.with_name(
        f".{workspace.name}{SubprocessTaskPublisher._STAGING_SUFFIX}"
    )
    assert caught.value.code == "workspace-staging-identity-changed"
    assert (staging / "FOREIGN.txt").read_text() == "must remain\n"
    assert publisher.displaced is not None and publisher.displaced.is_dir()
    assert not workspace.exists()
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 0


@pytest.mark.parametrize(
    "marker_phase", ["local_workspace", "committed_locally", "push_confirmed"]
)
@pytest.mark.parametrize(
    "interruption", ["temp_created", "temp_written", "temp_fsync", "before_replace"]
)
def test_marker_atomic_write_interruption_is_retry_safe_at_initial_and_later_phases(
    registry_factory, tmp_path, marker_phase, interruption
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = MarkerInterruptionPublisher(
        marker_phase=marker_phase, interruption=interruption
    )

    with pytest.raises(OperatorIntakeError):
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=workspace_root,
        receipt_path=tmp_path / "receipt.json",
        publisher=publisher,
    )

    workspace = Path(result["publication"]["workspace"])
    marker = workspace / ".git" / SubprocessTaskPublisher._MARKER_NAME
    marker_temporary = marker.with_name(
        marker.name + SubprocessTaskPublisher._MARKER_TEMP_SUFFIX
    )
    assert json.loads(marker.read_text())["phase"] == "pr_confirmed"
    assert not marker_temporary.exists()
    assert _git(workspace, "rev-list", "--count", f"{plan['registry']['commit']}..HEAD") == "1"
    assert sum(command[:2] == ("git", "clone") for command in publisher.commands) == 1
    assert _publication_commit_count(publisher.commands) == 1
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 1
    assert sum(command[:3] == ("gh", "pr", "create") for command in publisher.commands) == 1


@pytest.mark.parametrize("temporary_state", ["foreign", "symlink", "directory"])
def test_initial_marker_temporary_foreign_or_nonregular_state_blocks_without_recreation(
    registry_factory, tmp_path, temporary_state
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / plan["proposal_sha256"][:20]
    staging = workspace.with_name(
        f".{workspace.name}{SubprocessTaskPublisher._STAGING_SUFFIX}"
    )
    publisher = MarkerInterruptionPublisher(
        marker_phase="local_workspace", interruption="temp_created"
    )
    with pytest.raises(OperatorIntakeError):
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    marker = staging / ".git" / SubprocessTaskPublisher._MARKER_NAME
    temporary = marker.with_name(marker.name + SubprocessTaskPublisher._MARKER_TEMP_SUFFIX)
    if temporary_state == "foreign":
        temporary.write_bytes(b"foreign marker bytes\n")
    else:
        temporary.unlink()
        if temporary_state == "symlink":
            outside = tmp_path / "outside-marker"
            outside.write_text("unchanged\n")
            temporary.symlink_to(outside)
        else:
            temporary.mkdir()

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    assert caught.value.code in {
        "workspace-marker-temp-invalid",
        "workspace-marker-temp-mismatch",
    }
    assert staging.is_dir()
    assert not workspace.exists()
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 0


def test_interruption_after_workspace_setup_reconciles_staging_before_remote_effects(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = PreEffectInterruptionPublisher(
        interruption="before_workspace_rename"
    )

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    workspace = workspace_root / plan["proposal_sha256"][:20]
    staging = workspace.with_name(
        f".{workspace.name}{SubprocessTaskPublisher._STAGING_SUFFIX}"
    )
    assert caught.value.publication_phase == "local_workspace"
    assert not workspace.exists()
    assert staging.is_dir()
    marker = json.loads(
        (staging / ".git" / SubprocessTaskPublisher._MARKER_NAME).read_text()
    )
    assert marker["proposal_sha256"] == plan["proposal_sha256"]
    assert marker["phase"] == "local_workspace"

    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=workspace_root,
        receipt_path=tmp_path / "receipt.json",
        publisher=publisher,
    )

    assert result["publication"]["workspace_reconciled"] is True
    assert [path for path in workspace_root.iterdir() if path.is_dir()] == [workspace]
    assert not staging.exists()
    assert _git(workspace, "rev-list", "--count", f"{plan['registry']['commit']}..HEAD") == "1"
    assert sum(command[:2] == ("git", "clone") for command in publisher.commands) == 1
    assert _publication_commit_count(publisher.commands) == 1
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 1
    assert sum(command[:3] == ("gh", "pr", "create") for command in publisher.commands) == 1


@pytest.mark.parametrize(
    "interruption",
    ["target_temp_created", "target_temp_fsync", "target_renamed"],
)
def test_interruption_during_target_atomic_creation_reconciles_exact_reviewed_bytes(
    registry_factory, tmp_path, interruption
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = PreEffectInterruptionPublisher(interruption=interruption)

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    assert caught.value.publication_phase == "local_workspace"
    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=workspace_root,
        receipt_path=tmp_path / "receipt.json",
        publisher=publisher,
    )

    workspace = Path(result["publication"]["workspace"])
    target = workspace / plan["target_path"]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == plan["task_file_sha256"]
    assert [path for path in workspace_root.iterdir() if path.is_dir()] == [workspace]
    assert _git(workspace, "rev-list", "--count", f"{plan['registry']['commit']}..HEAD") == "1"
    assert sum(command[:2] == ("git", "clone") for command in publisher.commands) == 1
    assert _publication_commit_count(publisher.commands) == 1
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 1
    assert sum(command[:3] == ("gh", "pr", "create") for command in publisher.commands) == 1


def test_local_registry_validation_fails_before_publication_workspace_and_releases_leases(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(registry, store)
    invalid_verified_task = _task(registry.root)
    invalid_verified_task["state"] = "verified"
    plan_path = tmp_path / "proposal.json"
    task_propose(
        registry,
        store,
        candidate_id=recorded["candidate_id"],
        task_json=invalid_verified_task,
        publishing_task_id="BUR-TEST-001-T001",
        path=plan_path,
    )
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    resource_db = _lease_db(preview, tmp_path)
    workspace_root = tmp_path / "workspaces"
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=LocalGitPublisher(),
        )

    assert caught.value.code == "local-registry-validation-failed"
    assert caught.value.publication_phase == "before_workspace"
    assert caught.value.effect_started is False
    assert caught.value.ambiguity is False
    assert caught.value.details["lease_release"]["released"] is True
    assert _lease_count(resource_db) == 0
    assert not workspace_root.exists()


def test_t072_exact_pre_effect_workspace_is_reused_without_duplicate_effects(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    resource_db = _lease_db(preview, tmp_path)
    workspace_root = tmp_path / "workspaces"
    publisher = FaultInjectingLocalPublisher(fail_after=("git", "add"))

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    assert caught.value.code == "local-publication-failed"
    assert caught.value.publication_phase == "local_workspace"
    assert caught.value.effect_started is False
    assert caught.value.details["lease_release"]["released"] is True
    assert _lease_count(resource_db) == 0

    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=workspace_root,
        receipt_path=tmp_path / "receipt.json",
        publisher=publisher,
    )
    assert result["publication_phase"] == "pr_confirmed"
    assert result["publication"]["workspace_reconciled"] is True
    assert sum(command[:2] == ("git", "clone") for command in publisher.commands) == 1
    assert _publication_commit_count(publisher.commands) == 1
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 1
    assert sum(command[:3] == ("gh", "pr", "create") for command in publisher.commands) == 1


def test_fault_after_commit_reconciles_exactly_one_commit_across_retry(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = FaultInjectingLocalPublisher(fail_after=("git", "update-ref"))

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    workspace = workspace_root / plan["proposal_sha256"][:20]
    assert caught.value.code == "local-publication-failed"
    assert caught.value.publication_phase == "local_workspace"
    assert _git(workspace, "rev-list", "--count", f"{plan['registry']['commit']}..HEAD") == "1"

    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=workspace_root,
        receipt_path=tmp_path / "receipt.json",
        publisher=publisher,
    )

    assert result["publication"]["workspace_reconciled"] is True
    assert _git(workspace, "rev-list", "--count", f"{plan['registry']['commit']}..HEAD") == "1"
    assert _publication_commit_count(publisher.commands) == 1


def test_fault_after_gh_pr_create_reuses_exact_pr_readback_across_retry(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    resource_db = _lease_db(preview, tmp_path)
    publisher = FaultInjectingLocalPublisher(fail_after=("gh", "pr", "create"))

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    assert caught.value.code == "publication-unclear"
    assert caught.value.publication_phase == "pr_attempted"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert publisher.pull_request is not None

    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=resource_db,
        workspace_root=tmp_path / "workspaces",
        receipt_path=tmp_path / "receipt.json",
        publisher=publisher,
    )

    readback = result["publication"]["pull_request"]
    assert sum(command[:3] == ("gh", "pr", "create") for command in publisher.commands) == 1
    assert readback == publisher.pull_request
    assert readback["headRefOid"] == result["publication"]["head"]
    assert readback["headRefName"] == result["branch"]
    assert readback["baseRefName"] == "main"
    assert readback["state"] == "OPEN"


@pytest.mark.parametrize("target_kind", ["symlink", "directory"])
def test_workspace_reconciliation_rejects_non_regular_target_without_following(
    registry_factory, tmp_path, target_kind
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = FaultInjectingLocalPublisher(fail_after=("git", "add"))
    with pytest.raises(OperatorIntakeError):
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    workspace = workspace_root / plan["proposal_sha256"][:20]
    target = workspace / plan["target_path"]
    target.unlink()
    if target_kind == "symlink":
        outside = tmp_path / "outside-target.json"
        outside.write_text("must not be followed\n")
        target.symlink_to(outside)
    else:
        target.mkdir()

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    assert caught.value.code == "workspace-target-type-invalid"
    assert caught.value.publication_phase == "local_workspace"
    assert caught.value.effect_started is False
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 0


def test_unbound_existing_workspace_blocks_before_remote_effect(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / plan["proposal_sha256"][:20]
    workspace.mkdir(parents=True)
    publisher = LocalGitPublisher()

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    assert caught.value.code == "workspace-identity-ambiguous"
    assert caught.value.publication_phase == "before_workspace"
    assert publisher.pull_request is None


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("dirty", "workspace-local-state-mismatch"),
        ("foreign", "workspace-identity-mismatch"),
    ],
)
def test_dirty_or_foreign_pre_effect_workspace_is_never_reused(
    registry_factory, tmp_path, mutation, expected_code
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    workspace_root = tmp_path / "workspaces"
    publisher = FaultInjectingLocalPublisher(fail_after=("git", "add"))
    with pytest.raises(OperatorIntakeError):
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    workspace = workspace_root / plan["proposal_sha256"][:20]
    if mutation == "dirty":
        (workspace / "FOREIGN.txt").write_text("foreign state\n")
    else:
        marker_path = workspace / ".git" / SubprocessTaskPublisher._MARKER_NAME
        marker = json.loads(marker_path.read_text())
        marker["proposal_sha256"] = "0" * 64
        marker_path.write_text(json.dumps(marker))

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=workspace_root,
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    assert caught.value.code == expected_code
    assert caught.value.effect_started is False
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 0


def test_post_push_ambiguity_reconciles_remote_head_without_duplicate_push_or_pr(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    resource_db = _lease_db(preview, tmp_path)
    publisher = FaultInjectingLocalPublisher(fail_after=("git", "push"))

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    assert caught.value.code == "publication-unclear"
    assert caught.value.publication_phase == "push_attempted"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert _lease_count(resource_db) == len(preview["required_resource_keys"])

    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=resource_db,
        workspace_root=tmp_path / "workspaces",
        receipt_path=tmp_path / "receipt.json",
        publisher=publisher,
    )
    assert result["publication_phase"] == "pr_confirmed"
    assert _lease_count(resource_db) == 0
    assert sum(command[:2] == ("git", "push") for command in publisher.commands) == 1
    assert sum(command[:3] == ("gh", "pr", "create") for command in publisher.commands) == 1


@pytest.mark.parametrize(
    ("fault_after", "retry_disruption", "expected_phase"),
    [
        (("git", "push"), "remote-main-advance", "push_attempted"),
        (("gh", "pr", "create"), "ls-remote-fault", "pr_attempted"),
    ],
)
def test_remote_phase_is_restored_before_retry_preflight_and_leases_are_preserved(
    registry_factory,
    tmp_path,
    fault_after,
    retry_disruption,
    expected_phase,
):
    root, registry = _committed_registry(registry_factory)
    remote = _local_remote(root, tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    resource_db = _lease_db(preview, tmp_path)
    publisher = FaultInjectingLocalPublisher(fail_after=fault_after)

    with pytest.raises(OperatorIntakeError):
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )
    if retry_disruption == "remote-main-advance":
        _advance_remote_main(remote, tmp_path)
    else:
        publisher.fail_after = ("git", "ls-remote")
        publisher.injected = False

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=publisher,
        )

    assert caught.value.publication_phase == expected_phase
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert "lease_release" not in caught.value.details
    assert _lease_count(resource_db) == len(preview["required_resource_keys"])


def test_known_local_typed_failure_is_preserved_and_is_not_publication_unclear(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    resource_db = _lease_db(preview, tmp_path)
    original = OperatorIntakeError("injected-local-code", "known local failure")
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=FakePublisher(fail=original, fail_phase="local_workspace"),
        )
    assert caught.value is original
    assert caught.value.code == "injected-local-code"
    assert caught.value.publication_phase == "local_workspace"
    assert caught.value.effect_started is False
    assert caught.value.details["lease_release"]["released"] is True


@pytest.mark.parametrize(
    ("effect_started", "ambiguity"),
    [(True, False), (False, True)],
)
def test_pre_remote_error_flags_independently_prevent_lease_release(
    registry_factory, tmp_path, effect_started, ambiguity
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    resource_db = _lease_db(preview, tmp_path)
    original = OperatorIntakeError(
        "injected-possible-effect",
        "failure carries possible remote effect evidence",
        effect_started=effect_started,
        ambiguity=ambiguity,
    )

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=resource_db,
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
            publisher=FakePublisher(fail=original, fail_phase="local_workspace"),
        )

    assert caught.value is original
    assert "lease_release" not in caught.value.details
    assert _lease_count(resource_db) == len(preview["required_resource_keys"])


def _cli_result(capsys) -> dict:
    output = json.loads(capsys.readouterr().out)
    return output.get("result", output)


def test_cli_adapters_preserve_domain_results_without_extra_authority(
    registry_factory, tmp_path, capsys
):
    root, _ = _committed_registry(registry_factory)
    state_db = tmp_path / "cli-state.sqlite3"
    request_path = tmp_path / "candidate-request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "idempotency_key": "cli:operator-intake",
                "title": "CLI candidate adapter",
                "source_kind": "test-fixture",
                "source_locator": "fixture:cli",
                "source_sha256": "c" * 64,
                "desired_outcome": "Prove thin CLI adapter behavior",
                "repo": "repo.alpha",
            }
        )
    )
    common = [
        "--root",
        str(root),
        "--state-db",
        str(state_db),
        "--json",
        "--json-envelope",
    ]
    assert (
        bureau_cli.main([*common, "operator-candidate-record", "--request", str(request_path)]) == 0
    )
    recorded = _cli_result(capsys)
    assert recorded["kind"] == "bureau_candidate_record_result"
    assert recorded["status"] == "recorded"

    assert (
        bureau_cli.main(
            [
                *common,
                "operator-candidate-assess",
                "--candidate-id",
                recorded["candidate_id"],
                "--initiative",
                "BUR-TEST-001",
                "--task-id",
                "BUR-TEST-001-T099",
            ]
        )
        == 0
    )
    assessed = _cli_result(capsys)
    assert assessed["kind"] == "bureau_candidate_assessment"
    assert assessed["decision"] == "promote"
    assert assessed["advisory_only"] is True

    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(_task(root), indent=2) + "\n")
    plan_path = tmp_path / "cli-proposal.json"
    assert (
        bureau_cli.main(
            [
                *common,
                "operator-task-propose",
                "--candidate-id",
                recorded["candidate_id"],
                "--task-json",
                str(task_path),
                "--publishing-task-id",
                "BUR-TEST-001-T001",
                "--write-plan",
                str(plan_path),
            ]
        )
        == 0
    )
    proposed = _cli_result(capsys)
    assert proposed["kind"] == "bureau_task_proposal_result"
    _review(plan_path)

    assert (
        bureau_cli.main([*common, "operator-task-publish", "--plan", str(plan_path), "--preview"])
        == 0
    )
    preview = _cli_result(capsys)
    assert preview["kind"] == "bureau_task_publication_preview"
    assert preview["effect_started"] is False
    assert "queue_mutation" in preview["does_not_establish"]


def test_cli_emits_typed_operator_intake_failure(registry_factory, tmp_path, capsys):
    root, _ = _committed_registry(registry_factory)
    state_db = tmp_path / "cli-state.sqlite3"
    request_path = tmp_path / "invalid-request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "idempotency_key": "invalid key with spaces",
                "title": "Invalid",
                "source_kind": "fixture",
                "desired_outcome": "Must fail before append",
                "repo": "repo.alpha",
            }
        )
    )
    rc = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state_db),
            "--json",
            "--json-envelope",
            "operator-candidate-record",
            "--request",
            str(request_path),
        ]
    )
    assert rc == 2
    failure = _cli_result(capsys)
    assert failure["kind"] == "bureau_operator_intake_failure"
    assert failure["code"] == "idempotency-key-invalid"
    assert failure["effect_started"] is False
    assert failure["required_readback"] == []


def test_cli_missing_candidate_request_is_typed_failure(registry_factory, tmp_path, capsys):
    root, _ = _committed_registry(registry_factory)
    rc = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(tmp_path / "state.sqlite3"),
            "--json",
            "--json-envelope",
            "operator-candidate-record",
            "--request",
            str(tmp_path / "missing.json"),
        ]
    )
    assert rc == 2
    failure = _cli_result(capsys)
    assert failure["kind"] == "bureau_operator_intake_failure"
    assert failure["code"] == "request-read-failed"
    assert failure["effect_started"] is False


def test_cli_non_object_task_json_is_typed_failure(registry_factory, tmp_path, capsys):
    root, _ = _committed_registry(registry_factory)
    task_path = tmp_path / "task.json"
    task_path.write_text("[]\n")
    rc = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(tmp_path / "state.sqlite3"),
            "--json",
            "--json-envelope",
            "operator-task-propose",
            "--candidate-id",
            "candidate-missing",
            "--task-json",
            str(task_path),
            "--publishing-task-id",
            "BUR-TEST-001-T001",
            "--write-plan",
            str(tmp_path / "proposal.json"),
        ]
    )
    assert rc == 2
    failure = _cli_result(capsys)
    assert failure["kind"] == "bureau_operator_intake_failure"
    assert failure["code"] == "task-object-required"
    assert failure["effect_started"] is False
