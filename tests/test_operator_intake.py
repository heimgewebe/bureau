from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import operator_intake as operator_intake_module
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
    def __init__(self, *, mutate_plan: Path | None = None, fail: Exception | None = None):
        self.calls = 0
        self.mutate_plan = mutate_plan
        self.fail = fail

    def publish(self, *, registry, plan, workspace_root, assert_plan_unchanged):
        self.calls += 1
        assert_plan_unchanged()
        if self.mutate_plan is not None:
            self.mutate_plan.write_text(self.mutate_plan.read_text() + " ")
        if self.fail is not None:
            raise self.fail
        assert_plan_unchanged()
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


def test_publication_receipt_write_failure_is_ambiguous_after_remote_effect(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    blocked_parent = tmp_path / "receipt-parent-is-file"
    blocked_parent.write_text("not a directory")
    publisher = FakePublisher()
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "workspaces",
            receipt_path=blocked_parent / "receipt.json",
            publisher=publisher,
        )
    assert publisher.calls == 1
    assert caught.value.code == "receipt-write-unclear"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert any("publication receipt" in item for item in caught.value.required_readback)


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
    @staticmethod
    def _github_slug(remote: str) -> str:
        return "example/bureau"

    def _run(self, arguments, *, cwd=None, timeout=60):
        if list(arguments[:3]) == ["gh", "pr", "create"]:
            return "https://example.invalid/pull/7"
        if list(arguments[:3]) == ["gh", "pr", "view"]:
            assert cwd is not None
            head = super()._run(["git", "rev-parse", "HEAD"], cwd=cwd)
            branch = super()._run(["git", "branch", "--show-current"], cwd=cwd)
            return json.dumps(
                {
                    "number": 7,
                    "url": "https://example.invalid/pull/7",
                    "state": "OPEN",
                    "headRefOid": head,
                    "headRefName": branch,
                    "baseRefName": "main",
                }
            )
        return super()._run(arguments, cwd=cwd, timeout=timeout)


def test_subprocess_publisher_creates_only_target_branch_and_task_file(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
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
