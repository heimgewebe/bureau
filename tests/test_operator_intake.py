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
from bureau import task_specs as task_specs_module
from bureau.core import Registry, StateStore
from bureau.live_register import live_register_record
from bureau.operator_intake import (
    OperatorIntakeError,
    candidate_assess,
    candidate_record,
    candidate_record_request,
    candidate_record_request_contract,
    publication_preview,
    publish_task_proposal,
    review_task_proposal,
    task_propose,
)
from bureau.registry_snapshot import snapshot_tree_sha256
from bureau.source_discovery import (
    candidate_request as source_candidate_request,
)
from bureau.source_discovery import (
    record_candidate as record_source_candidate,
)
from bureau.source_discovery import (
    record_discovery_candidate,
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
                "assertion": "The exact reviewed candidate implementation is merged.",
                "evidence_type": "object",
                "verifier": "code_merged",
                "verifier_config": {
                    "repository": "heimgewebe/bureau",
                    "pull_request": 7,
                    "head_sha": "a" * 40,
                    "base_ref": "main",
                },
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
    store.import_registry_task_specs(registry)
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


def _revision_proposal(
    registry: Registry,
    store: StateStore,
    tmp_path: Path,
    *,
    task_id: str = "BUR-TEST-001-T002",
    candidate_task_id: str | None = None,
    key: str = "source:revision",
) -> tuple[Path, dict, dict]:
    store.import_registry_task_specs(registry)
    candidate_target = task_id if candidate_task_id is None else candidate_task_id
    recorded = candidate_record(
        registry,
        store,
        idempotency_key=key,
        title=f"Revise {task_id}",
        source_kind="runtime-diagnostic",
        source_locator=f"bureau:{task_id}",
        source_sha256="9" * 64,
        desired_outcome=f"Revise the exact existing TaskSpec {task_id}",
        repo="repo.alpha",
        task_id=candidate_target,
    )
    revised = json.loads(json.dumps(registry.tasks[task_id].raw))
    revised["title"] = f"{revised['title']} revised through operator intake"
    plan_path = tmp_path / f"{task_id}.revision.proposal.json"
    task_propose(
        registry,
        store,
        candidate_id=recorded["candidate_id"],
        task_json=revised,
        publishing_task_id="BUR-TEST-001-T001",
        path=plan_path,
    )
    return plan_path, recorded, revised


def _state_store_only_revision_proposal(
    registry: Registry,
    store: StateStore,
    tmp_path: Path,
    *,
    task_id: str = "BUR-TEST-001-T099",
) -> tuple[Path, dict, dict, dict]:
    store.import_registry_task_specs(registry)
    seeded = store.put_task_spec(
        _task(registry.root, task_id),
        idempotency_key=f"seed:{task_id}",
        expected_revision=None,
        source="test-state-store-only-task",
    )
    assert task_id not in registry.tasks
    baseline = store.task_spec(task_id)
    assert baseline is not None
    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:state-store-only-revision",
        title=f"Revise StateStore-only {task_id}",
        source_kind="runtime-diagnostic",
        source_locator=f"bureau:state-store-only:{task_id}",
        source_sha256="3" * 64,
        desired_outcome=f"Revise the exact authoritative TaskSpec {task_id}",
        repo="repo.alpha",
        task_id=task_id,
    )
    revised = json.loads(json.dumps(baseline["spec"]))
    revised["title"] = f"{revised['title']} revised from StateStore authority"
    plan_path = tmp_path / f"{task_id}.state-store-only.proposal.json"
    task_propose(
        registry,
        store,
        candidate_id=recorded["candidate_id"],
        task_json=revised,
        publishing_task_id="BUR-TEST-001-T001",
        path=plan_path,
    )
    return plan_path, recorded, seeded, revised


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
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES('resource_lease_contract_version', '1')"
    )
    lease_metadata: dict[str, object] = {
        "task_id": "BUR-TEST-001-T001",
        "operation": "state-task-publication",
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
        ttl = 1800
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


@pytest.mark.parametrize(
    "source_kind",
    [
        "conversation",
        "github-issue",
        "source-observer",
        "doctor",
        "local-fallback",
    ],
)
def test_all_source_adapters_emit_the_canonical_candidate_event_identity(source_kind):
    request = source_candidate_request(
        source_kind=source_kind,
        source_locator=f"{source_kind}:stable-locator",
        source_sha256="1" * 64,
        title="Canonical candidate identity",
        desired_outcome="Record one durable candidate event",
        repo="repo.alpha",
    )

    assert request["idempotency_key"].startswith("candidate:")
    assert len(request["idempotency_key"].split(":")) == 3
    assert request["catalog_validation"] == "deferred"


def test_discovery_finding_uses_the_same_candidate_contract(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    title = "Unify the discovered candidate path"
    outcome = "Route the discovery finding through durable operator intake"
    observed = record_discovery_candidate(
        registry,
        store,
        {
            "fingerprint": "8" * 64,
            "source_id": "repo:bureau",
            "source_revision": "9" * 40,
            "source_path": "docs/tasks.md",
            "source_anchor": "L42",
            "summary": title,
            "target_outcome": outcome,
        },
        repo="repo.alpha",
        catalog_validation="strict",
    )
    conversation = record_source_candidate(
        registry,
        store,
        source_kind="conversation",
        source_locator="chat:t008:discovery-equivalent",
        source_sha256="a" * 64,
        title=title,
        desired_outcome=outcome,
        repo="repo.alpha",
        catalog_validation="strict",
    )

    assert observed["candidate_id"] == conversation["candidate_id"]
    assert observed["content_fingerprint"] == conversation["content_fingerprint"]
    assert observed["source_fingerprint"] != conversation["source_fingerprint"]
    assert len(operator_intake_module.current_candidate_records(store)) == 1


def test_equivalent_cross_source_findings_share_one_candidate_history(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    finding = {
        "title": "Repair the deterministic lease readback",
        "desired_outcome": "Classify the same lease readback failure deterministically",
        "repo": "repo.alpha",
        "catalog_validation": "strict",
    }
    conversation = record_source_candidate(
        registry,
        store,
        source_kind="conversation",
        source_locator="chat:t008:lease-readback",
        source_sha256="2" * 64,
        **finding,
    )
    issue = record_source_candidate(
        registry,
        store,
        source_kind="github-issue",
        source_locator="github:heimgewebe/bureau#2008",
        source_sha256="3" * 64,
        **finding,
    )

    assert conversation["candidate_id"] == issue["candidate_id"]
    assert conversation["content_fingerprint"] == issue["content_fingerprint"]
    assert conversation["source_fingerprint"] != issue["source_fingerprint"]
    assert conversation["candidate_event_id"] != issue["candidate_event_id"]
    assert issue["record"]["supersedes_event_id"] == conversation["event_id"]
    assert len(operator_intake_module.current_candidate_records(store)) == 1
    observations = issue["record"]["operator_intake"]["source_observations"]
    assert {item["kind"] for item in observations} == {"conversation", "github-issue"}

    replay = record_source_candidate(
        registry,
        store,
        source_kind="conversation",
        source_locator="chat:t008:lease-readback",
        source_sha256="2" * 64,
        **finding,
    )
    assert replay["idempotent_replay"] is True
    assert replay["event_id"] == issue["event_id"]
    assert replay["source_event_id"] == conversation["event_id"]
    assert replay["candidate_event_id"] == conversation["candidate_event_id"]
    assert len(operator_intake_module.candidate_records(store)) == 2


def test_cross_source_evidence_cannot_reopen_a_promoted_candidate(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    finding = {
        "title": "Preserve reviewed candidate lifecycle",
        "desired_outcome": "Keep later source evidence from reopening promotion",
        "repo": "repo.alpha",
        "catalog_validation": "strict",
    }
    first = record_source_candidate(
        registry,
        store,
        source_kind="doctor",
        source_locator="doctor:t008:lifecycle",
        source_sha256="6" * 64,
        **finding,
    )
    promoted = live_register_record(
        registry,
        store,
        kind="candidate_task",
        title=first["record"]["title"],
        candidate_id=first["candidate_id"],
        supersedes_event_id=first["event_id"],
        status="promoted",
        promotion_required=False,
    )
    observed_again = record_source_candidate(
        registry,
        store,
        source_kind="github-issue",
        source_locator="github:heimgewebe/bureau#2010",
        source_sha256="7" * 64,
        **finding,
    )

    assert observed_again["record"]["supersedes_event_id"] == promoted["event_id"]
    assert observed_again["record"]["status"] == "promoted"
    assert observed_again["record"]["promotion_required"] is False


def test_shared_source_distinct_findings_remain_independently_reviewable(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    shared_source = {
        "source_kind": "github-issue",
        "source_locator": "github:heimgewebe/bureau#2009",
        "source_sha256": "4" * 64,
        "repo": "repo.alpha",
        "catalog_validation": "strict",
    }
    first = record_source_candidate(
        registry,
        store,
        title="Repair candidate replay",
        desired_outcome="Make candidate replay idempotent",
        **shared_source,
    )
    second = record_source_candidate(
        registry,
        store,
        title="Bound candidate authority",
        desired_outcome="Keep candidate assessment advisory",
        **shared_source,
    )

    assert first["source_fingerprint"] == second["source_fingerprint"]
    assert first["content_fingerprint"] != second["content_fingerprint"]
    assert first["candidate_id"] != second["candidate_id"]
    assert len(operator_intake_module.current_candidate_records(store)) == 2
    assert (
        candidate_assess(registry, store, candidate_id=first["candidate_id"])["decision"]
        == "promote"
    )
    assert (
        candidate_assess(registry, store, candidate_id=second["candidate_id"])["decision"]
        == "promote"
    )


def test_local_fallback_survives_offline_intake_and_later_idempotent_sync(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    finding = {
        "source_kind": "local-fallback",
        "source_locator": "local:doctor-spool:t008",
        "source_sha256": "5" * 64,
        "title": "Persist the offline Doctor finding",
        "desired_outcome": "Replay the local finding after connectors recover",
        "repo": "repo.alpha",
    }
    offline = record_source_candidate(None, store, **finding)

    def fail_registry_snapshot(_registry):
        raise AssertionError("deferred synchronization must not require GitHub or Registry I/O")

    monkeypatch.setattr(
        operator_intake_module,
        "_canonical_read_registry_snapshot",
        fail_registry_snapshot,
    )
    synchronized = record_source_candidate(registry, store, **finding)

    assert offline["record"]["catalog_validation"]["status"] == "deferred"
    assert synchronized["idempotent_replay"] is True
    assert synchronized["event_id"] == offline["event_id"]
    assert synchronized["candidate_event_id"] == offline["candidate_event_id"]
    assert len(operator_intake_module.candidate_records(store)) == 1


def test_operator_intake_accepts_strict_acs_binding_and_rejects_unknown_repo(
    registry_factory, tmp_path
):
    root, _registry = _committed_registry(registry_factory)
    source = Path(__file__).resolve().parents[1]
    shutil.copy2(
        source / "registry/resources/agent-control-surface.json",
        root / "registry/resources/agent-control-surface.json",
    )
    _git(root, "add", "registry/resources/agent-control-surface.json")
    _git(root, "commit", "-m", "catalogue ACS fixture")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)

    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:acs-resource-parity",
        title="Implement ACS repository work",
        source_kind="registry-live-audit",
        source_locator="systemkatalog:repo:agent-control-surface",
        source_sha256="a" * 64,
        desired_outcome="Bind an ACS task to its exact repository resource",
        repo="repo.agent-control-surface",
    )

    assert recorded["record"]["repo"] == "repo.agent-control-surface"
    assert recorded["record"]["catalog_validation"]["status"] == "validated"
    with pytest.raises(OperatorIntakeError, match="unknown live register repo") as caught:
        candidate_record(
            registry,
            store,
            idempotency_key="source:unknown-acs-resource",
            title="Reject unknown repository work",
            source_kind="registry-live-audit",
            desired_outcome="Reject a missing repository binding",
            repo="repo.unknown-acs",
        )
    assert caught.value.code == "candidate-record-invalid"
    assert len(operator_intake_module.candidate_records(store)) == 1

    task = _task(root)
    task["claims"] = [
        {
            "resource": "repo.agent-control-surface",
            "mode": "write",
            "isolation": "worktree",
        }
    ]
    task["required_capabilities"] = ["repository", "shell", "git", "github"]
    plan_path = tmp_path / "acs-proposal.json"
    task_propose(
        registry,
        store,
        candidate_id=recorded["candidate_id"],
        task_json=task,
        publishing_task_id="BUR-TEST-001-T001",
        path=plan_path,
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["task_json"]["claims"] == task["claims"]
    assert plan["task_json"]["required_capabilities"] == [
        "repository",
        "shell",
        "git",
        "github",
    ]


def test_candidate_record_request_contract_is_machine_readable() -> None:
    contract = candidate_record_request_contract()

    assert contract["schema_version"] == 1
    assert contract["kind"] == "bureau_candidate_record_request_contract"
    assert (
        contract["request_schema_version"]
        == operator_intake_module.OPERATOR_INTAKE_SCHEMA_VERSION
    )
    assert contract["required_fields"] == sorted(
        {
            "schema_version",
            "idempotency_key",
            "title",
            "source_kind",
            "desired_outcome",
        }
    )
    assert set(contract["required_fields"]).isdisjoint(contract["optional_fields"])
    assert sorted(contract["required_fields"] + contract["optional_fields"]) == contract[
        "allowed_fields"
    ]
    assert contract["allowed_fields"] == sorted(
        operator_intake_module._CANDIDATE_RECORD_REQUEST_FIELDS
    )
    assert contract["defaults"] == {"catalog_validation": "strict"}
    close = contract["operations"]["close"]
    assert close["operation"] == "close"
    assert close["outcome"] == "completed"
    assert close["max_evidence"] == operator_intake_module.MAX_CANDIDATE_CLOSE_EVIDENCE
    assert set(close["required_fields"]) == (
        operator_intake_module._CANDIDATE_CLOSE_REQUEST_REQUIRED_FIELDS
    )
    assert set(close["allowed_fields"]) == operator_intake_module._CANDIDATE_CLOSE_REQUEST_FIELDS
    assert set(close["evidence_sources"]) == (
        operator_intake_module._CANDIDATE_CLOSE_EVIDENCE_SOURCES
    )


def test_candidate_record_request_contract_failures_are_actionable(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    with pytest.raises(OperatorIntakeError) as schema_error:
        candidate_record_request(None, store, {"schema_version": 2})

    assert schema_error.value.code == "request-schema-unsupported"
    assert schema_error.value.retryable is False
    assert schema_error.value.details == {
        "expected_schema_version": 1,
        "received_schema_version": 2,
    }

    with pytest.raises(OperatorIntakeError) as fields_error:
        candidate_record_request(
            None,
            store,
            {
                "schema_version": 1,
                "unexpected_z": "z",
                "unexpected_a": "a",
            },
        )

    assert fields_error.value.code == "request-fields-unknown"
    assert fields_error.value.retryable is False
    assert fields_error.value.details == {
        "unknown_fields": ["unexpected_a", "unexpected_z"],
        "allowed_fields": candidate_record_request_contract()["allowed_fields"],
    }

    with pytest.raises(OperatorIntakeError) as record_missing_error:
        candidate_record_request(None, store, {"schema_version": 1})

    assert record_missing_error.value.code == "request-fields-missing"
    assert record_missing_error.value.retryable is False
    assert record_missing_error.value.effect_started is False
    assert record_missing_error.value.details == {
        "missing_fields": [
            "desired_outcome",
            "idempotency_key",
            "source_kind",
            "title",
        ]
    }

    with pytest.raises(OperatorIntakeError) as operation_error:
        candidate_record_request(
            None,
            store,
            {"schema_version": 1, "operation": ["close"]},
        )
    assert operation_error.value.code == "request-operation-invalid"

    with pytest.raises(OperatorIntakeError) as missing_error:
        candidate_record_request(
            None,
            store,
            {
                "schema_version": 1,
                "operation": "close",
                "candidate_id": "candidate-example",
            },
        )
    assert missing_error.value.code == "request-fields-missing"
    assert missing_error.value.details["missing_fields"] == [
        "evidence",
        "expected_event_id",
        "idempotency_key",
        "outcome",
    ]


def test_candidate_record_request_contract_cli_does_not_load_registry(
    monkeypatch, tmp_path, capsys
) -> None:
    def fail_registry_load(_root):
        raise AssertionError("operator-candidate-contract must not load the Git registry")

    monkeypatch.setattr(bureau_cli.Registry, "load", fail_registry_load)

    rc = bureau_cli.main(
        [
            "--root",
            str(tmp_path / "missing-checkout"),
            "--json",
            "operator-candidate-contract",
        ]
    )

    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "bureau_candidate_record_request_contract"
    assert result["request_schema_version"] == 1
    assert "runtime_identity" in result


def test_candidate_record_preserves_v1_request_hash_without_refinement(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    recorded = _record(registry, store)
    expected_request = {
        "schema_version": 1,
        "idempotency_key": "source:alpha",
        "title": "Create exact operator intake task",
        "source_kind": "conversation",
        "desired_outcome": "Create a typed and reviewed Bureau task publication path",
        "repo": "repo.alpha",
        "source_locator": "chat:alpha",
        "source_sha256": "a" * 64,
        "observed_at": None,
        "task_id": None,
        "candidate_id": None,
        "note": None,
        "catalog_validation": "strict",
    }

    assert recorded["request_sha256"] == operator_intake_module.legacy.sha256_json(expected_request)
    assert "supersedes_event_id" not in expected_request


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
        status="promoted",
        promotion_required=False,
        supersedes_event_id=first["event_id"],
        note="Corrected wording without creating a new candidate identity",
    )
    replay = _record(registry, store)
    assert replay["candidate_id"] == first["candidate_id"]
    assert replay["event_id"] == correction["event_id"]
    assert correction["record"]["operator_intake"] == first["record"]["operator_intake"]
    result = candidate_assess(registry, store, candidate_id=first["candidate_id"])
    assert result["candidate_status"] == "promoted"
    assert result["missing_fields"] == []
    assert result["source_freshness"] == {
        "status": "digest-bound",
        "observed_at": first["record"]["operator_intake"]["source"]["observed_at"],
        "sha256": "a" * 64,
        "catalog_validation": correction["record"]["catalog_validation"],
    }
    assert result["exact_duplicates"] == []
    assert not any(
        item.get("id") == first["candidate_id"] for item in result["similarity_suggestions"]
    )


def test_candidate_assess_resolves_idempotency_key_to_current_candidate(registry_factory, tmp_path):
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
        status="promoted",
        promotion_required=False,
        supersedes_event_id=first["event_id"],
        note="Corrected wording without creating a new candidate identity",
    )

    assessed = candidate_assess(
        registry,
        store,
        idempotency_key="source:alpha",
    )
    assert assessed["candidate_id"] == first["candidate_id"]
    assert assessed["event_id"] == correction["event_id"]
    assert assessed["candidate_status"] == "promoted"

    with pytest.raises(OperatorIntakeError) as invalid:
        candidate_assess(registry, store, idempotency_key="invalid key")
    assert invalid.value.code == "idempotency-key-invalid"

    with pytest.raises(OperatorIntakeError) as mixed:
        candidate_assess(
            registry,
            store,
            candidate_id=first["candidate_id"],
            idempotency_key="source:alpha",
        )
    assert mixed.value.code == "candidate-selector-invalid"


def test_candidate_assess_rejects_unknown_idempotency_key(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    with pytest.raises(OperatorIntakeError) as caught:
        candidate_assess(registry, store, idempotency_key="source:missing")
    assert caught.value.code == "idempotency-key-unknown"


def test_candidate_request_refines_current_event_and_inherits_identity(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    request = {
        "schema_version": 1,
        "idempotency_key": "source:alpha-refinement",
        "title": "Refined exact operator intake task",
        "source_kind": "conversation",
        "source_locator": "chat:alpha-refinement",
        "source_sha256": "b" * 64,
        "desired_outcome": "Refine the existing source-bound candidate",
        "supersedes_event_id": first["event_id"],
    }

    refined = candidate_record_request(registry, store, request)
    replayed = candidate_record_request(registry, store, request)

    assert refined["status"] == "recorded"
    assert replayed["status"] == "existing"
    assert refined["candidate_id"] == first["candidate_id"]
    assert refined["event_id"] > first["event_id"]
    assert replayed["event_id"] == refined["event_id"]
    assert refined["record"]["supersedes_event_id"] == first["event_id"]
    assert refined["record"]["repo"] == "repo.alpha"
    assert refined["record"]["status"] == first["record"]["status"]
    assert refined["record"]["promotion_required"] == first["record"]["promotion_required"]
    assert refined["record"]["operator_intake"]["request_sha256"] == refined["request_sha256"]
    assert refined["record"]["operator_intake"]["desired_outcome"] == request["desired_outcome"]
    assert refined["record"]["operator_intake"]["source"]["sha256"] == "b" * 64
    assert refined["record"]["operator_intake"] != first["record"]["operator_intake"]


def _candidate_close_request(recorded: dict, *, key: str = "close:alpha") -> dict:
    return {
        "schema_version": 1,
        "operation": "close",
        "idempotency_key": key,
        "candidate_id": recorded["candidate_id"],
        "expected_event_id": recorded["event_id"],
        "outcome": "completed",
        "evidence": [
            {
                "source": "github",
                "reference": "heimgewebe/example#42@merge",
                "sha256": "c" * 64,
            },
            {
                "source": "test",
                "reference": "pytest:test_candidate_close",
                "sha256": "d" * 64,
            },
        ],
        "note": "Implementation merged and verified.",
    }


def test_candidate_request_closes_exact_current_candidate_with_bound_evidence(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    request = _candidate_close_request(first)

    closed = candidate_record_request(registry, store, request)
    assessed = candidate_assess(registry, store, candidate_id=first["candidate_id"])

    assert closed["kind"] == "bureau_candidate_close_result"
    assert closed["status"] == "closed"
    assert closed["effect_started"] is True
    assert closed["idempotent_replay"] is False
    assert closed["candidate_id"] == first["candidate_id"]
    assert closed["event_id"] > first["event_id"]
    assert closed["record"]["status"] == "closed"
    assert closed["record"]["promotion_required"] is False
    assert closed["record"]["supersedes_event_id"] == first["event_id"]
    closeout = closed["record"]["operator_intake"]["candidate_closeout"]
    assert closeout["kind"] == "bureau_candidate_closeout"
    assert closeout["candidate_id"] == first["candidate_id"]
    assert closeout["predecessor_event_id"] == first["event_id"]
    assert closeout["outcome"] == "completed"
    assert closeout["request_sha256"] == closed["request_sha256"]
    assert closeout["closeout_sha256"] == closed["closeout_sha256"]
    assert closeout["evidence"] == sorted(
        request["evidence"], key=operator_intake_module.legacy.canonical_json
    )
    assert closeout["evidence_sha256"] == operator_intake_module.legacy.sha256_json(
        closeout["evidence"]
    )
    assert assessed["candidate_status"] == "closed"
    assert assessed["decision"] == "drop"


def test_candidate_close_request_is_idempotent(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    request = _candidate_close_request(first)

    closed = candidate_record_request(registry, store, request)
    replayed = candidate_record_request(registry, store, request)

    assert replayed["status"] == "closed"
    assert replayed["effect_started"] is False
    assert replayed["idempotent_replay"] is True
    assert replayed["event_id"] == closed["event_id"]
    assert replayed["request_sha256"] == closed["request_sha256"]
    assert replayed["closeout_sha256"] == closed["closeout_sha256"]
    assert replayed["does_not_establish"] == closed["does_not_establish"]


def test_candidate_close_rejects_stale_event_without_effect(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    refined = candidate_record_request(
        registry,
        store,
        {
            "schema_version": 1,
            "idempotency_key": "source:alpha-before-close",
            "title": "Refined candidate before close",
            "source_kind": "conversation",
            "source_locator": "chat:alpha-refined",
            "source_sha256": "e" * 64,
            "desired_outcome": "Refine before close",
            "candidate_id": first["candidate_id"],
            "supersedes_event_id": first["event_id"],
        },
    )
    request = _candidate_close_request(first)

    with pytest.raises(OperatorIntakeError) as caught:
        candidate_record_request(registry, store, request)

    assert caught.value.code == "candidate-close-stale"
    assert caught.value.effect_started is False
    assert caught.value.required_readback == ("candidate_by_candidate_id",)
    assessed = candidate_assess(registry, store, candidate_id=first["candidate_id"])
    assert assessed["event_id"] == refined["event_id"]
    assert assessed["candidate_status"] == "observed"


def test_candidate_close_rejects_different_reclose(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    closed = candidate_record_request(registry, store, _candidate_close_request(first))
    different = _candidate_close_request(first, key="close:alpha:different")
    different["evidence"][0]["sha256"] = "f" * 64

    with pytest.raises(OperatorIntakeError) as caught:
        candidate_record_request(registry, store, different)

    assert caught.value.code == "candidate-close-not-active"
    assert caught.value.details["candidate_status"] == "closed"
    assessed = candidate_assess(registry, store, candidate_id=first["candidate_id"])
    assert assessed["event_id"] == closed["event_id"]


def test_candidate_close_requires_sha_bound_supported_evidence(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)

    missing = _candidate_close_request(first)
    missing["evidence"] = []
    with pytest.raises(OperatorIntakeError) as missing_error:
        candidate_record_request(registry, store, missing)
    assert missing_error.value.code == "candidate-close-evidence-invalid"

    unsupported = _candidate_close_request(first)
    unsupported["evidence"][0]["source"] = "web"
    with pytest.raises(OperatorIntakeError) as source_error:
        candidate_record_request(registry, store, unsupported)
    assert source_error.value.code == "candidate-close-evidence-source-unsupported"

    invalid_digest = _candidate_close_request(first)
    invalid_digest["evidence"][0]["sha256"] = "ABC"
    with pytest.raises(OperatorIntakeError) as digest_error:
        candidate_record_request(registry, store, invalid_digest)
    assert digest_error.value.code == "candidate-close-evidence-digest-invalid"


def test_candidate_request_can_add_assessment_missing_repo_once(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = candidate_record(
        registry,
        store,
        idempotency_key="source:repo-late",
        title="Candidate awaiting repository binding",
        source_kind="conversation",
        source_locator="chat:repo-late",
        desired_outcome="Bind the repository requested by candidate assessment",
    )

    before = candidate_assess(registry, store, candidate_id=first["candidate_id"])
    assert before["decision"] == "refine"
    assert before["missing_fields"] == ["repo"]

    refined = candidate_record_request(
        registry,
        store,
        {
            "schema_version": 1,
            "idempotency_key": "source:repo-late-refinement",
            "title": "Candidate with repository binding",
            "source_kind": "conversation",
            "source_locator": "chat:repo-late",
            "desired_outcome": "Bind the repository requested by candidate assessment",
            "repo": "repo.alpha",
            "supersedes_event_id": first["event_id"],
        },
    )

    assert refined["candidate_id"] == first["candidate_id"]
    assert refined["record"]["repo"] == "repo.alpha"
    assert refined["record"]["status"] == first["record"]["status"]
    assert refined["record"]["promotion_required"] == first["record"]["promotion_required"]
    after = candidate_assess(registry, store, candidate_id=first["candidate_id"])
    assert after["decision"] == "promote"
    assert after["missing_fields"] == []
    assert after["target"]["claims"] == [
        {"resource": "repo.alpha", "mode": "write", "isolation": "worktree"}
    ]


def test_candidate_request_still_rejects_refinement_repo_rebinding(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)

    with pytest.raises(OperatorIntakeError, match="repo cannot change") as caught:
        candidate_record_request(
            registry,
            store,
            {
                "schema_version": 1,
                "idempotency_key": "source:repo-rebinding",
                "title": "Attempt repository rebinding",
                "source_kind": "conversation",
                "source_locator": "chat:repo-rebinding",
                "desired_outcome": "Reject changing an existing repository binding",
                "repo": "repo.beta",
                "supersedes_event_id": first["event_id"],
            },
        )

    assert caught.value.code == "candidate-record-invalid"
    assert caught.value.effect_started is False
    assert len(operator_intake_module.candidate_records(store)) == 1


def test_candidate_request_rejects_refinement_task_rebinding(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    task_ids = sorted(registry.tasks)
    assert len(task_ids) >= 2
    store = StateStore(tmp_path / "state.sqlite3")
    first = candidate_record(
        registry,
        store,
        idempotency_key="source:task-bound-candidate",
        title="Task-bound candidate",
        source_kind="conversation",
        desired_outcome="Preserve one exact task binding",
        repo="repo.alpha",
        task_id=task_ids[0],
    )

    with pytest.raises(OperatorIntakeError, match="task cannot change") as caught:
        candidate_record_request(
            registry,
            store,
            {
                "schema_version": 1,
                "idempotency_key": "source:task-rebinding-attempt",
                "title": "Invalid task rebinding",
                "source_kind": "conversation",
                "desired_outcome": "Attempt to replace the predecessor task",
                "task_id": task_ids[1],
                "supersedes_event_id": first["event_id"],
            },
        )

    assert caught.value.code == "candidate-record-invalid"
    assert caught.value.effect_started is False
    assert len(operator_intake_module.candidate_records(store)) == 1


def test_candidate_request_strictly_revalidates_inherited_deferred_bindings(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = candidate_record(
        registry,
        store,
        idempotency_key="source:deferred-missing-repo",
        title="Deferred candidate",
        source_kind="conversation",
        desired_outcome="Record before the repository is catalogued",
        repo="repo.missing",
        catalog_validation="deferred",
    )

    with pytest.raises(OperatorIntakeError, match="unknown live register repo") as caught:
        candidate_record_request(
            registry,
            store,
            {
                "schema_version": 1,
                "idempotency_key": "source:strict-refinement",
                "title": "Strict refinement",
                "source_kind": "conversation",
                "desired_outcome": "Revalidate inherited bindings",
                "supersedes_event_id": first["event_id"],
            },
        )

    assert caught.value.code == "candidate-record-invalid"
    assert caught.value.effect_started is False
    assert len(operator_intake_module.candidate_records(store)) == 1


@pytest.mark.parametrize("value", [True, False, 0, -1, "1", 1.0])
def test_candidate_request_rejects_invalid_supersedes_event_id(registry_factory, tmp_path, value):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    with pytest.raises(OperatorIntakeError) as caught:
        candidate_record_request(
            registry,
            store,
            {
                "schema_version": 1,
                "idempotency_key": "source:invalid-refinement",
                "title": "Invalid refinement",
                "source_kind": "conversation",
                "desired_outcome": "Reject an invalid predecessor binding",
                "repo": "repo.alpha",
                "supersedes_event_id": value,
            },
        )

    assert caught.value.code == "supersedes-event-id-invalid"
    assert caught.value.effect_started is False
    assert operator_intake_module.candidate_records(store) == []


def test_candidate_refinement_idempotency_binds_superseded_event(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = _record(registry, store)
    request = {
        "schema_version": 1,
        "idempotency_key": "source:refinement-binding",
        "title": "Bound candidate refinement",
        "source_kind": "conversation",
        "desired_outcome": "Bind refinement to one predecessor event",
        "supersedes_event_id": first["event_id"],
    }
    candidate_record_request(registry, store, request)

    with pytest.raises(OperatorIntakeError) as caught:
        candidate_record_request(
            registry,
            store,
            {**request, "supersedes_event_id": first["event_id"] + 1000},
        )

    assert caught.value.code == "idempotency-conflict"
    assert caught.value.effect_started is False


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
    assert caught.value.details == {
        "unknown_fields": ["invented_authority"],
        "allowed_fields": sorted(operator_intake_module._CANDIDATE_RECORD_REQUEST_FIELDS),
    }


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


def test_candidate_assessment_reports_shared_source_as_advisory(registry_factory, tmp_path):
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
        desired_outcome="Implement a different result from the shared review artifact",
        repo="repo.beta",
    )
    result = candidate_assess(registry, store, candidate_id=second["candidate_id"])
    assert result["decision"] == "promote"
    assert result["exact_duplicates"] == []
    assert result["source_relationships"] == [
        {
            "kind": "candidate-source-digest",
            "candidate_id": first["candidate_id"],
            "event_id": first["event_id"],
            "reason": "same source_sha256",
            "identity_equivalent": False,
            "same_repository": False,
            "same_desired_outcome": False,
            "same_explicit_task_id": False,
        }
    ]
    assert result["source_relationships_summary"] == {
        "total_count": 1,
        "returned_count": 1,
        "truncated": False,
    }


def test_candidate_assessment_keeps_explicit_task_identity_exact(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    first = candidate_record(
        registry,
        store,
        idempotency_key="task-identity:first",
        candidate_id="candidate-task-identity-first",
        title="First task-bound candidate",
        source_kind="conversation",
        source_locator="chat:task-identity:first",
        source_sha256="1" * 64,
        desired_outcome="Keep the first explicit task binding",
        repo="repo.alpha",
        task_id="BUR-TEST-001-T001",
    )
    second = candidate_record(
        registry,
        store,
        idempotency_key="task-identity:second",
        candidate_id="candidate-task-identity-second",
        title="Second task-bound candidate",
        source_kind="conversation",
        source_locator="chat:task-identity:second",
        source_sha256="2" * 64,
        desired_outcome="Attempt a second binding to the same explicit task",
        repo="repo.beta",
        task_id="BUR-TEST-001-T001",
    )

    result = candidate_assess(registry, store, candidate_id=second["candidate_id"])

    assert result["decision"] == "merge"
    assert {finding["kind"] for finding in result["exact_duplicates"]} == {
        "candidate-task-id",
        "task-id",
    }
    candidate_finding = next(
        finding for finding in result["exact_duplicates"] if finding["kind"] == "candidate-task-id"
    )
    assert candidate_finding["candidate_id"] == first["candidate_id"]
    assert result["source_relationships"] == []


def test_candidate_assessment_bounds_shared_source_relationships(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    for index in range(operator_intake_module.MAX_SOURCE_RELATIONSHIPS + 5):
        candidate_record(
            registry,
            store,
            idempotency_key=f"shared-source:{index}",
            title=f"Shared source candidate {index}",
            source_kind="pull-request-diff",
            source_locator="github:heimgewebe/weltgewebe#1489",
            source_sha256="8" * 64,
            desired_outcome=f"Implement independent outcome {index}",
            repo="repo.alpha" if index % 2 == 0 else "repo.beta",
        )
    target = candidate_record(
        registry,
        store,
        idempotency_key="shared-source:target",
        title="Shared source target",
        source_kind="pull-request-diff",
        source_locator="github:heimgewebe/weltgewebe#1489",
        source_sha256="8" * 64,
        desired_outcome="Implement the final independent outcome",
        repo="repo.beta",
    )

    result = candidate_assess(registry, store, candidate_id=target["candidate_id"])

    assert result["decision"] == "promote"
    assert len(result["source_relationships"]) == operator_intake_module.MAX_SOURCE_RELATIONSHIPS
    assert result["source_relationships_summary"] == {
        "total_count": operator_intake_module.MAX_SOURCE_RELATIONSHIPS + 5,
        "returned_count": operator_intake_module.MAX_SOURCE_RELATIONSHIPS,
        "truncated": True,
    }


def test_shared_source_candidates_keep_independent_reviewed_proposals(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    source_sha256 = "7" * 64
    requests = [
        {
            "idempotency_key": "source:740",
            "title": "Weltgewebe validation profile migration",
            "desired_outcome": "Replace legacy validation commands in Weltgewebe",
            "repo": "repo.alpha",
            "task_id": "BUR-TEST-001-T097",
        },
        {
            "idempotency_key": "source:741",
            "title": "Weltgewebe workflow dependency pinning",
            "desired_outcome": "Hash-pin the workflow dependency set",
            "repo": "repo.alpha",
            "task_id": "BUR-TEST-001-T098",
        },
        {
            "idempotency_key": "source:742",
            "title": "Grabowski operation lifecycle",
            "desired_outcome": "Implement a typed operation lifecycle in Grabowski",
            "repo": "repo.beta",
            "task_id": "BUR-TEST-001-T099",
        },
    ]
    recorded = [
        candidate_record(
            registry,
            store,
            idempotency_key=request["idempotency_key"],
            title=request["title"],
            source_kind="pull-request-diff",
            source_locator="github:heimgewebe/weltgewebe#1489",
            source_sha256=source_sha256,
            desired_outcome=request["desired_outcome"],
            repo=request["repo"],
        )
        for request in requests
    ]

    previews = []
    for request, candidate in zip(requests, recorded, strict=True):
        assessment = candidate_assess(
            registry,
            store,
            candidate_id=candidate["candidate_id"],
            initiative="BUR-TEST-001",
            task_id=request["task_id"],
        )
        assert assessment["decision"] == "promote"
        assert assessment["exact_duplicates"] == []
        assert len(assessment["source_relationships"]) == 2

        task = _task(root, request["task_id"])
        task["title"] = request["title"]
        task["goal"] = request["desired_outcome"]
        task["claims"] = [{"resource": request["repo"], "mode": "write", "isolation": "worktree"}]
        proposal_path = tmp_path / f"{request['task_id']}.proposal.json"
        task_propose(
            registry,
            store,
            task_json=task,
            publishing_task_id="BUR-TEST-001-T001",
            path=proposal_path,
            candidate_id=candidate["candidate_id"],
        )
        _review(proposal_path)
        previews.append(publication_preview(registry, store, plan_path=proposal_path))

    assert [preview["task_id"] for preview in previews] == [
        request["task_id"] for request in requests
    ]
    assert len({preview["proposal_sha256"] for preview in previews}) == 3


def test_task_revision_proposal_binds_authoritative_baseline(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path, recorded, _ = _revision_proposal(registry, store, tmp_path)

    plan = json.loads(plan_path.read_text())
    baseline = store.task_spec(plan["task_id"])
    assert baseline is not None
    assert plan["task_id"] in registry.tasks
    assert plan["candidate"]["candidate_id"] == recorded["candidate_id"]
    assert plan["task_spec"]["operation"] == "revise"
    assert plan["task_spec"]["expected_revision"] == 1
    assert plan["task_spec"]["expected_spec_sha256"] == baseline["spec_sha256"]
    assert plan["task_spec"]["proposed_spec_sha256"] == plan["task_json_sha256"]
    target = root / plan["target_path"]
    assert (
        plan["task_spec"]["expected_task_file_sha256"]
        == hashlib.sha256(target.read_bytes()).hexdigest()
    )
    _review(plan_path)
    assert publication_preview(registry, store, plan_path=plan_path)["status"] == "ready"


def test_authoritative_candidate_catalog_overlays_one_typed_task(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    task_id = "BUR-TEST-001-T099"
    store.put_task_spec(
        _task(registry.root, task_id),
        idempotency_key=f"seed:{task_id}",
        expected_revision=None,
        source="test-state-store-only-task",
    )
    authoritative = store.task_spec(task_id)
    assert authoritative is not None
    lookups = []

    def exact_task_spec(requested_task_id):
        lookups.append(requested_task_id)
        return authoritative

    monkeypatch.setattr(store, "task_spec", exact_task_spec)

    catalog = operator_intake_module._authoritative_candidate_catalog(registry, store, task_id)

    assert lookups == [task_id]
    assert catalog is not registry
    assert catalog.tasks is not registry.tasks
    assert task_id not in registry.tasks
    assert isinstance(catalog.tasks[task_id], operator_intake_module.legacy.Task)
    assert catalog.tasks[task_id].id == task_id
    assert catalog.tasks[task_id].raw == authoritative["spec"]


def test_authoritative_candidate_catalog_preserves_existing_git_task(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    task_id = sorted(registry.tasks)[0]

    def unexpected_task_spec(_task_id):
        pytest.fail("existing Git task must not query the StateStore overlay")

    monkeypatch.setattr(store, "task_spec", unexpected_task_spec)

    catalog = operator_intake_module._authoritative_candidate_catalog(registry, store, task_id)

    assert catalog is registry
    assert isinstance(catalog.tasks[task_id], operator_intake_module.legacy.Task)


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("digest", "authoritative TaskSpec digest drift"),
        ("id", "authoritative TaskSpec id drift"),
    ],
)
def test_authoritative_candidate_catalog_rejects_digest_or_id_drift(
    registry_factory, tmp_path, monkeypatch, drift, message
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    task_id = "BUR-TEST-001-T099"
    spec = _task(registry.root, task_id)
    if drift == "id":
        spec["id"] = "BUR-TEST-001-T098"
    digest = operator_intake_module.task_specs.task_spec_digest(spec)
    if drift == "digest":
        digest = "0" * 64
    monkeypatch.setattr(
        store,
        "task_spec",
        lambda requested_task_id: {
            "spec": spec,
            "spec_sha256": digest,
        }
        if requested_task_id == task_id
        else None,
    )

    with pytest.raises(operator_intake_module.StateError, match=message):
        operator_intake_module._authoritative_candidate_catalog(registry, store, task_id)

    assert task_id not in registry.tasks


def test_task_revision_resolves_state_store_only_authoritative_task(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")

    plan_path, recorded, seeded, _ = _state_store_only_revision_proposal(registry, store, tmp_path)

    plan = json.loads(plan_path.read_text())
    assert recorded["record"]["task_id"] == plan["task_id"]
    assert recorded["record"]["catalog_validation"]["status"] == "validated"
    assert plan["task_spec"]["operation"] == "revise"
    assert plan["task_spec"]["expected_revision"] == seeded["revision"]
    assert plan["task_spec"]["expected_spec_sha256"] == seeded["spec_sha256"]
    assert plan["task_spec"]["proposed_spec_sha256"] == plan["task_json_sha256"]
    assert plan["task_spec"]["expected_task_file_sha256"] is None


def test_candidate_refinement_inherits_state_store_only_task_for_strict_validation(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    task_id = "BUR-TEST-001-T099"
    store.put_task_spec(
        _task(registry.root, task_id),
        idempotency_key=f"seed:{task_id}",
        expected_revision=None,
        source="test-state-store-only-task",
    )
    assert task_id not in registry.tasks
    first = candidate_record(
        registry,
        store,
        idempotency_key="source:state-store-only-candidate",
        title=f"Observe StateStore-only {task_id}",
        source_kind="runtime-diagnostic",
        source_locator=f"bureau:state-store-only:{task_id}",
        source_sha256="5" * 64,
        desired_outcome=f"Refine the authoritative TaskSpec {task_id}",
        repo="repo.alpha",
        task_id=task_id,
    )

    refined = candidate_record(
        registry,
        store,
        idempotency_key="source:state-store-only-candidate-refinement",
        title=f"Refine StateStore-only {task_id}",
        source_kind="runtime-diagnostic",
        source_locator=f"bureau:state-store-only:{task_id}:refinement",
        source_sha256="6" * 64,
        desired_outcome=f"Refine the authoritative TaskSpec {task_id}",
        supersedes_event_id=first["event_id"],
    )

    assert refined["candidate_id"] == first["candidate_id"]
    assert refined["record"]["task_id"] == task_id
    assert refined["record"]["supersedes_event_id"] == first["event_id"]
    assert refined["record"]["catalog_validation"] == {
        "mode": "strict",
        "status": "validated",
        "does_not_establish": [],
    }


def test_candidate_record_rejects_task_unknown_to_registry_and_state_store(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)

    with pytest.raises(OperatorIntakeError, match="unknown live register task") as caught:
        candidate_record(
            registry,
            store,
            idempotency_key="source:truly-unknown-task",
            title="Reject a truly unknown task binding",
            source_kind="runtime-diagnostic",
            source_locator="bureau:unknown-task",
            source_sha256="4" * 64,
            desired_outcome="Keep strict candidate task binding fail-closed",
            repo="repo.alpha",
            task_id="BUR-TEST-001-T777",
        )

    assert caught.value.code == "candidate-record-invalid"
    assert caught.value.effect_started is False
    assert operator_intake_module.candidate_records(store) == []


def test_state_store_only_revision_rejects_candidate_bound_to_another_task(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T099"
    store.put_task_spec(
        _task(registry.root, task_id),
        idempotency_key=f"seed:{task_id}",
        expected_revision=None,
        source="test-state-store-only-task",
    )
    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:state-store-only-wrong-target",
        title="Reject a mismatched StateStore-only revision candidate",
        source_kind="runtime-diagnostic",
        source_locator="bureau:state-store-only-wrong-target",
        source_sha256="5" * 64,
        desired_outcome="Keep the candidate bound to its exact existing task",
        repo="repo.alpha",
        task_id="BUR-TEST-001-T001",
    )
    baseline = store.task_spec(task_id)
    assert baseline is not None
    revised = json.loads(json.dumps(baseline["spec"]))
    revised["title"] = "Must not revise a differently bound StateStore-only task"
    plan_path = tmp_path / "state-store-only-mismatch.json"

    with pytest.raises(OperatorIntakeError) as caught:
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=revised,
            publishing_task_id="BUR-TEST-001-T001",
            path=plan_path,
        )

    assert caught.value.code == "candidate-task-identity-mismatch"
    assert not plan_path.exists()


def test_task_revision_allows_git_projection_to_lag_authoritative_baseline(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    initial = store.task_spec(task_id)
    assert initial is not None
    authoritative = json.loads(json.dumps(initial["spec"]))
    authoritative["state"] = "ready"
    authoritative["title"] = f"{authoritative['title']} with authoritative StateStore evidence"
    advanced = store.put_task_spec(
        authoritative,
        idempotency_key="authoritative-lifecycle-advance",
        expected_revision=initial["revision"],
        source="test-lifecycle-reconcile",
    )
    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:revision-with-lagging-git-projection",
        title="Revise from authoritative StateStore baseline",
        source_kind="runtime-diagnostic",
        source_locator="bureau:lagging-git-projection",
        source_sha256="4" * 64,
        desired_outcome="Revise the exact task without downgrading authoritative lifecycle truth",
        repo="repo.alpha",
        task_id=task_id,
    )
    revised = json.loads(json.dumps(authoritative))
    revised["title"] = f"{revised['title']} revised after lifecycle advance"
    plan_path = tmp_path / "lagging-git-projection.revision.proposal.json"

    task_propose(
        registry,
        store,
        candidate_id=recorded["candidate_id"],
        task_json=revised,
        publishing_task_id="BUR-TEST-001-T001",
        path=plan_path,
    )

    plan = json.loads(plan_path.read_text())
    assert plan["task_spec"]["operation"] == "revise"
    assert plan["task_spec"]["expected_revision"] == advanced["revision"]
    assert plan["task_spec"]["expected_spec_sha256"] == advanced["spec_sha256"]
    assert plan["task_spec"]["expected_spec_sha256"] != initial["spec_sha256"]
    assert plan["task_json"]["state"] == "ready"
    target = root / plan["target_path"]
    assert (
        plan["task_spec"]["expected_task_file_sha256"]
        == hashlib.sha256(target.read_bytes()).hexdigest()
    )
    _review(plan_path)
    assert publication_preview(registry, store, plan_path=plan_path)["status"] == "ready"


def test_task_revision_rejects_candidate_bound_to_another_task(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:wrong-revision-target",
        title="Wrong revision target",
        source_kind="runtime-diagnostic",
        source_locator="bureau:wrong-target",
        source_sha256="8" * 64,
        desired_outcome="Attempt to revise another task",
        repo="repo.alpha",
        task_id="BUR-TEST-001-T001",
    )
    revised = json.loads(json.dumps(registry.tasks[task_id].raw))
    revised["title"] = "Must not be silently revised"
    with pytest.raises(OperatorIntakeError) as caught:
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=revised,
            publishing_task_id="BUR-TEST-001-T001",
            path=tmp_path / "wrong-target.json",
        )
    assert caught.value.code == "candidate-task-identity-mismatch"
    assert not (tmp_path / "wrong-target.json").exists()


def test_task_revision_ignores_terminal_candidate_bound_to_same_task(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    prior = candidate_record(
        registry,
        store,
        idempotency_key="source:terminal-prior-revision",
        title="Prior completed revision candidate",
        source_kind="runtime-diagnostic",
        source_locator="bureau:terminal-prior",
        source_sha256="7" * 64,
        desired_outcome="Represent an already handled revision request",
        repo="repo.alpha",
        task_id=task_id,
    )
    live_register_record(
        registry,
        store,
        kind="candidate_task",
        title="Prior completed revision candidate",
        source="operator-intake-test",
        candidate_id=prior["candidate_id"],
        supersedes_event_id=prior["event_id"],
        status="promoted",
        promotion_required=False,
        note="Already handled; must not block later reviewed revisions.",
    )

    plan_path, current, _ = _revision_proposal(
        registry,
        store,
        tmp_path,
        task_id=task_id,
        key="source:revision-after-terminal-candidate",
    )

    plan = json.loads(plan_path.read_text())
    assert plan["candidate"]["candidate_id"] == current["candidate_id"]
    assert plan["task_spec"]["operation"] == "revise"
    assert not any(
        finding.get("kind") == "candidate-task-id"
        and finding.get("candidate_id") == prior["candidate_id"]
        for finding in plan["assessment"]["exact_duplicates"]
    )


def test_task_revision_still_rejects_other_active_candidate_for_same_task(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    prior = candidate_record(
        registry,
        store,
        idempotency_key="source:active-prior-revision",
        title="Competing active revision candidate",
        source_kind="runtime-diagnostic",
        source_locator="bureau:active-prior",
        source_sha256="6" * 64,
        desired_outcome="Keep this competing revision request open",
        repo="repo.alpha",
        task_id=task_id,
    )

    with pytest.raises(OperatorIntakeError) as caught:
        _revision_proposal(
            registry,
            store,
            tmp_path,
            task_id=task_id,
            key="source:revision-with-active-competitor",
        )

    assert caught.value.code == "exact-duplicate"
    assert any(
        finding.get("kind") == "candidate-task-id"
        and finding.get("candidate_id") == prior["candidate_id"]
        for finding in caught.value.details["findings"]
    )


def test_task_revision_still_rejects_paused_candidate_for_same_task(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    prior = candidate_record(
        registry,
        store,
        idempotency_key="source:paused-prior-revision",
        title="Paused competing revision candidate",
        source_kind="runtime-diagnostic",
        source_locator="bureau:paused-prior",
        source_sha256="5" * 64,
        desired_outcome="Keep this revision request on hold without terminating it",
        repo="repo.alpha",
        task_id=task_id,
    )
    live_register_record(
        registry,
        store,
        kind="candidate_task",
        title="Paused competing revision candidate",
        source="operator-intake-test",
        candidate_id=prior["candidate_id"],
        supersedes_event_id=prior["event_id"],
        status="paused",
        promotion_required=True,
        note="Paused is non-terminal and must continue to block competing revisions.",
    )

    with pytest.raises(OperatorIntakeError) as caught:
        _revision_proposal(
            registry,
            store,
            tmp_path,
            task_id=task_id,
            key="source:revision-with-paused-competitor",
        )

    assert caught.value.code == "exact-duplicate"
    assert any(
        finding.get("kind") == "candidate-task-id"
        and finding.get("candidate_id") == prior["candidate_id"]
        for finding in caught.value.details["findings"]
    )


def test_task_revision_publication_advances_and_replays_without_receipt(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path, _, _ = _revision_proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    first = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces-first",
        receipt_path=tmp_path / "receipt-first.json",
    )
    second = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces-replay",
        receipt_path=tmp_path / "receipt-replay.json",
    )
    assert first["task_spec_revision"]["revision"] == 2
    assert first["task_spec_revision"]["parent_revision"] == 1
    assert first["task_spec_revision"]["idempotent_replay"] is False
    assert second["task_spec_revision"]["revision"] == 2
    assert second["task_spec_revision"]["idempotent_replay"] is True
    assert second["legacy_task_spec_import"]["status"] == "retired"
    assert (
        store.task_spec(first["task_id"])["spec_sha256"]
        == first["task_spec_revision"]["spec_sha256"]
    )


def test_state_store_only_revision_publication_advances_and_replays_idempotently(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path, _, seeded, _ = _state_store_only_revision_proposal(registry, store, tmp_path)
    pending = json.loads(plan_path.read_text())
    review_task_proposal(
        plan_path=plan_path,
        reviewer="operator-state-store-only-review",
        expected_proposal_sha256=pending["proposal_sha256"],
    )
    preview = publication_preview(registry, store, plan_path=plan_path)

    first = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "unused-first",
        receipt_path=tmp_path / "state-store-only-first-receipt.json",
    )
    second = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "unused-replay",
        receipt_path=tmp_path / "state-store-only-replay-receipt.json",
    )

    assert first["publication_mode"] == "state_store"
    assert first["queue_mutated"] is False
    assert first["task_spec_revision"]["revision"] == seeded["revision"] + 1
    assert first["task_spec_revision"]["parent_revision"] == seeded["revision"]
    assert first["task_spec_revision"]["idempotent_replay"] is False
    assert second["task_spec_revision"]["revision"] == first["task_spec_revision"]["revision"]
    assert second["task_spec_revision"]["spec_sha256"] == first["task_spec_revision"]["spec_sha256"]
    assert second["task_spec_revision"]["idempotent_replay"] is True


def test_task_revision_stale_expected_revision_fails_before_publication_effect(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path, _, _ = _revision_proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    plan = json.loads(plan_path.read_text())
    current = store.task_spec(plan["task_id"])
    assert current is not None
    foreign = json.loads(json.dumps(current["spec"]))
    foreign["title"] = "Foreign revision wins the CAS race"
    store.put_task_spec(
        foreign,
        idempotency_key="foreign-revision",
        expected_revision=current["revision"],
        source="test-foreign-revision",
    )
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
        )
    assert caught.value.code == "task-spec-baseline-drift"
    assert store.task_spec(plan["task_id"])["spec"]["title"] == (
        "Foreign revision wins the CAS race"
    )


def test_state_store_only_revision_stale_expected_revision_fails_closed(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path, _, _, _ = _state_store_only_revision_proposal(registry, store, tmp_path)
    pending = json.loads(plan_path.read_text())
    review_task_proposal(
        plan_path=plan_path,
        reviewer="operator-state-store-only-review",
        expected_proposal_sha256=pending["proposal_sha256"],
    )
    preview = publication_preview(registry, store, plan_path=plan_path)
    current = store.task_spec(pending["task_id"])
    assert current is not None
    foreign = json.loads(json.dumps(current["spec"]))
    foreign["title"] = "Foreign StateStore-only revision wins the CAS race"
    advanced = store.put_task_spec(
        foreign,
        idempotency_key="foreign-state-store-only-revision",
        expected_revision=current["revision"],
        source="test-foreign-state-store-only-revision",
    )
    receipt_path = tmp_path / "state-store-only-stale-receipt.json"

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "unused-stale",
            receipt_path=receipt_path,
        )

    assert caught.value.code == "task-spec-baseline-drift"
    assert caught.value.effect_started is False
    assert not receipt_path.exists()
    authoritative = store.task_spec(pending["task_id"])
    assert authoritative is not None
    assert authoritative["revision"] == advanced["revision"]
    assert authoritative["spec_sha256"] == advanced["spec_sha256"]


def test_task_review_binds_exact_pending_proposal_and_enables_preview(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    pending = json.loads(plan_path.read_text())

    result = review_task_proposal(
        plan_path=plan_path,
        reviewer="ChatGPT through Grabowski",
        expected_proposal_sha256=pending["proposal_sha256"],
    )

    assert result["status"] == "reviewed"
    assert result["effect_started"] is True
    assert result["ambiguity"] is False
    assert result["review"]["reviewed_proposal_sha256"] == pending["proposal_sha256"]
    assert result["approval"]["allowed"] is True
    assert result["plan_file_sha256_before"] != result["plan_file_sha256_after"]
    preview = publication_preview(registry, store, plan_path=plan_path)
    assert preview["status"] == "ready"


def test_candidate_assessment_and_review_never_escalate_execution_authority(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    candidate = operator_intake_module.current_candidate_records(store)[0]
    assessment = candidate_assess(
        registry,
        store,
        candidate_id=candidate["record"]["candidate_id"],
    )
    pending = json.loads(plan_path.read_text(encoding="utf-8"))
    reviewed = review_task_proposal(
        plan_path=plan_path,
        reviewer="operator-self-review",
        expected_proposal_sha256=pending["proposal_sha256"],
    )
    required_nonclaims = {
        "claim_authority",
        "dispatch_authority",
        "merge_authority",
        "deployment_authority",
    }

    assert required_nonclaims <= set(candidate["record"]["candidate_event"]["does_not_establish"])
    assert required_nonclaims <= set(assessment["does_not_establish"])
    assert required_nonclaims <= set(pending["does_not_establish"])
    assert required_nonclaims <= set(reviewed["does_not_establish"])
    assert assessment["advisory_only"] is True


def test_task_review_exact_replay_is_idempotent(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    proposal_sha256 = json.loads(plan_path.read_text())["proposal_sha256"]
    first = review_task_proposal(
        plan_path=plan_path,
        reviewer="operator-self-review",
        expected_proposal_sha256=proposal_sha256,
    )

    replay = review_task_proposal(
        plan_path=plan_path,
        reviewer="operator-self-review",
        expected_proposal_sha256=proposal_sha256,
    )

    assert first["status"] == "reviewed"
    assert replay["status"] == "existing"
    assert replay["effect_started"] is False
    assert replay["idempotent_replay"] is True
    assert replay["plan_file_sha256_before"] == first["plan_file_sha256_after"]


def test_task_review_rejects_reference_unresolved_and_conflicting_reviewer(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    pending = json.loads(plan_path.read_text())
    initial_bytes = plan_path.read_bytes()

    with pytest.raises(OperatorIntakeError) as mismatch:
        review_task_proposal(
            plan_path=plan_path,
            reviewer="operator-self-review",
            expected_proposal_sha256="f" * 64,
        )
    assert mismatch.value.code == "proposal-review-reference-mismatch"
    assert plan_path.read_bytes() == initial_bytes

    pending["unresolved_fields"] = ["acceptance.runtime"]
    unsigned = {
        key: value for key, value in pending.items() if key not in {"proposal_sha256", "review"}
    }
    from bureau.legacy import sha256_json

    pending["proposal_sha256"] = sha256_json(unsigned)
    plan_path.write_text(json.dumps(pending, indent=2) + "\n")
    unresolved_bytes = plan_path.read_bytes()
    with pytest.raises(OperatorIntakeError) as unresolved:
        review_task_proposal(
            plan_path=plan_path,
            reviewer="operator-self-review",
            expected_proposal_sha256=pending["proposal_sha256"],
        )
    assert unresolved.value.code == "proposal-unresolved"
    assert plan_path.read_bytes() == unresolved_bytes

    pending["unresolved_fields"] = []
    unsigned = {
        key: value for key, value in pending.items() if key not in {"proposal_sha256", "review"}
    }
    pending["proposal_sha256"] = sha256_json(unsigned)
    plan_path.write_text(json.dumps(pending, indent=2) + "\n")
    review_task_proposal(
        plan_path=plan_path,
        reviewer="first-reviewer",
        expected_proposal_sha256=pending["proposal_sha256"],
    )
    reviewed_bytes = plan_path.read_bytes()
    with pytest.raises(OperatorIntakeError) as conflict:
        review_task_proposal(
            plan_path=plan_path,
            reviewer="second-reviewer",
            expected_proposal_sha256=pending["proposal_sha256"],
        )
    assert conflict.value.code == "review-conflict"
    assert plan_path.read_bytes() == reviewed_bytes


def test_task_review_cas_restores_foreign_pre_exchange_bytes(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    proposal_sha256 = json.loads(plan_path.read_text())["proposal_sha256"]
    foreign = json.loads(plan_path.read_text())
    foreign["review"]["foreign_marker"] = True
    foreign_bytes = (json.dumps(foreign, indent=2) + "\n").encode()

    def replace_before_exchange(path: Path) -> None:
        path.write_bytes(foreign_bytes)

    monkeypatch.setattr(
        operator_intake_module,
        "_before_proposal_review_exchange",
        replace_before_exchange,
    )
    with pytest.raises(OperatorIntakeError) as caught:
        review_task_proposal(
            plan_path=plan_path,
            reviewer="operator-self-review",
            expected_proposal_sha256=proposal_sha256,
        )

    assert caught.value.code == "proposal-review-conflict"
    assert caught.value.effect_started is False
    assert caught.value.details["rollback_complete"] is True
    assert plan_path.read_bytes() == foreign_bytes


def test_task_review_post_exchange_drift_is_ambiguous(registry_factory, tmp_path, monkeypatch):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    proposal_sha256 = json.loads(plan_path.read_text())["proposal_sha256"]
    foreign_bytes = b'{"foreign":true}\n'

    def replace_after_exchange(path: Path) -> None:
        path.write_bytes(foreign_bytes)

    monkeypatch.setattr(
        operator_intake_module,
        "_after_proposal_review_exchange",
        replace_after_exchange,
    )
    with pytest.raises(OperatorIntakeError) as caught:
        review_task_proposal(
            plan_path=plan_path,
            reviewer="operator-self-review",
            expected_proposal_sha256=proposal_sha256,
        )

    assert caught.value.code == "proposal-review-readback-ambiguous"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert caught.value.required_readback == (f"exact proposal bytes at {plan_path}",)
    assert plan_path.read_bytes() == foreign_bytes


def test_task_review_unexpected_post_exchange_failure_is_ambiguous(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    proposal_sha256 = json.loads(plan_path.read_text())["proposal_sha256"]

    def fail_after_exchange(path: Path) -> None:
        raise RuntimeError(f"unexpected readback failure for {path}")

    monkeypatch.setattr(
        operator_intake_module,
        "_after_proposal_review_exchange",
        fail_after_exchange,
    )
    with pytest.raises(OperatorIntakeError) as caught:
        review_task_proposal(
            plan_path=plan_path,
            reviewer="operator-self-review",
            expected_proposal_sha256=proposal_sha256,
        )

    assert caught.value.code == "proposal-review-effect-ambiguous"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert caught.value.required_readback == (f"exact proposal bytes at {plan_path}",)
    assert caught.value.details["error_type"] == "RuntimeError"


def test_task_review_parent_swap_before_exchange_is_fail_closed(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_path = _proposal(registry, store, plan_dir)
    proposal_sha256 = json.loads(plan_path.read_text())["proposal_sha256"]
    original_bytes = plan_path.read_bytes()
    moved_dir = tmp_path / "plans-moved"
    foreign_bytes = b'{"foreign":true}\n'

    def swap_parent(path: Path) -> None:
        path.parent.rename(moved_dir)
        path.parent.mkdir()
        path.write_bytes(foreign_bytes)

    monkeypatch.setattr(
        operator_intake_module,
        "_before_proposal_review_exchange",
        swap_parent,
    )
    with pytest.raises(OperatorIntakeError) as caught:
        review_task_proposal(
            plan_path=plan_path,
            reviewer="operator-self-review",
            expected_proposal_sha256=proposal_sha256,
        )

    assert caught.value.code == "proposal-review-parent-changed"
    assert caught.value.effect_started is False
    assert (moved_dir / plan_path.name).read_bytes() == original_bytes
    assert plan_path.read_bytes() == foreign_bytes


def test_task_review_parent_swap_after_exchange_is_ambiguous(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_path = _proposal(registry, store, plan_dir)
    proposal_sha256 = json.loads(plan_path.read_text())["proposal_sha256"]
    moved_dir = tmp_path / "plans-moved"
    foreign_bytes = b'{"foreign":true}\n'

    def swap_parent(path: Path) -> None:
        path.parent.rename(moved_dir)
        path.parent.mkdir()
        path.write_bytes(foreign_bytes)

    monkeypatch.setattr(
        operator_intake_module,
        "_after_proposal_review_exchange",
        swap_parent,
    )
    with pytest.raises(OperatorIntakeError) as caught:
        review_task_proposal(
            plan_path=plan_path,
            reviewer="operator-self-review",
            expected_proposal_sha256=proposal_sha256,
        )

    assert caught.value.code == "proposal-review-parent-ambiguous"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert f"directory identity for {plan_path.parent}" in caught.value.required_readback
    assert json.loads((moved_dir / plan_path.name).read_text())["review"]["status"] == "reviewed"
    assert plan_path.read_bytes() == foreign_bytes


def test_task_review_rejects_symlink_plan(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    proposal_sha256 = json.loads(plan_path.read_text())["proposal_sha256"]
    link = tmp_path / "proposal-link.json"
    link.symlink_to(plan_path)

    with pytest.raises(OperatorIntakeError) as caught:
        review_task_proposal(
            plan_path=link,
            reviewer="operator-self-review",
            expected_proposal_sha256=proposal_sha256,
        )

    assert caught.value.code == "proposal-type-invalid"


def test_publication_preview_rejects_symlink_plan(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    link = tmp_path / "reviewed-proposal-link.json"
    link.symlink_to(plan_path)

    with pytest.raises(OperatorIntakeError) as caught:
        publication_preview(registry, store, plan_path=link)

    assert caught.value.code == "proposal-type-invalid"


def test_publication_effect_rejects_symlink_plan_and_receipt(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    plan_link = tmp_path / "reviewed-proposal-effect-link.json"
    plan_link.symlink_to(plan_path)

    with pytest.raises(OperatorIntakeError) as plan_error:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_link,
            lease_binding={},
            workspace_root=tmp_path / "workspace",
            receipt_path=tmp_path / "receipt.json",
        )
    assert plan_error.value.code == "proposal-type-invalid"

    receipt_target = tmp_path / "foreign-receipt.json"
    receipt_target.write_text("{}\n")
    receipt_link = tmp_path / "receipt-link.json"
    receipt_link.symlink_to(receipt_target)
    with pytest.raises(OperatorIntakeError) as receipt_error:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding={},
            workspace_root=tmp_path / "workspace",
            receipt_path=receipt_link,
        )
    assert receipt_error.value.code == "receipt-type-invalid"


def test_task_proposal_binds_candidate_registry_and_review(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = json.loads(plan_path.read_text())
    assert plan["candidate"]["candidate_id"].startswith("candidate-")
    assert plan["registry"]["commit"] == _git(registry.root, "rev-parse", "HEAD")
    assert (
        plan["task_json"]["metadata"]["operator_intake"]["event_id"]
        == plan["candidate"]["event_id"]
    )
    assert plan["review"]["status"] == "pending"
    assert plan["publication"] == {
        "action_class": "registry_mutation",
        "publication_mode": "state_store",
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
    store.import_registry_task_specs(registry)
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


def test_task_proposal_rejects_pr1907_untyped_acceptance_before_write(
    registry_factory,
    tmp_path,
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    recorded = _record(registry, store)
    fixture = Path(__file__).with_name("fixtures") / "pr1907-untyped-task.json"
    task = _task(registry.root)
    task["acceptance"] = json.loads(fixture.read_text(encoding="utf-8"))["acceptance"]
    proposal_path = tmp_path / "proposal.json"

    with pytest.raises(OperatorIntakeError) as caught:
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=task,
            publishing_task_id="BUR-TEST-001-T001",
            path=proposal_path,
        )

    assert caught.value.code == "task-acceptance-contract-invalid"
    diagnostics = caught.value.details["acceptance_contract_errors"]
    assert caught.value.details["task_id"] == task["id"]
    assert len(diagnostics) == 15
    assert {item["task_id"] for item in diagnostics} == {task["id"]}
    assert {item["criterion_id"] for item in diagnostics} == {
        "required-checks-stay-strict",
        "platform-inconsistency-classified",
        "no-silent-ignore",
        "regression",
        "delivery",
    }
    first = diagnostics[0]
    assert first == {
        "task_id": task["id"],
        "criterion_id": "required-checks-stay-strict",
        "path": "$.acceptance[0].evidence_type",
        "code": "evidence-type-missing",
        "message": "evidence_type is required and must be exactly 'object'",
        "missing_fields": ["evidence_type"],
        "invalid_fields": [],
    }
    assert (
        f"task {task['id']} criterion required-checks-stay-strict "
        "at $.acceptance[0].evidence_type"
    ) in str(caught.value)
    assert "missing=evidence_type" in str(caught.value)
    assert not proposal_path.exists()
    assert store.task_spec(task["id"]) is None


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
    _review(plan_path)
    result = publication_preview(registry, store, plan_path=plan_path)
    assert result["status"] == "ready"
    assert result["approval"]["allowed"] is True
    assert result["publication_mode"] == "state_store"
    assert result["coordination_state_root"] == str(store.state_root.resolve())
    assert result["required_resource_keys"] == [f"path:{store.state_root.resolve()}"]
    assert result["branch"] is None
    assert result["open_pr_identity_revalidation"]["status"] == "not_required"


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
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path, omit=set(preview["required_resource_keys"])),
            workspace_root=tmp_path / "workspaces",
            receipt_path=tmp_path / "receipt.json",
        )
    assert caught.value.code == "lease-resources-missing"


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
        )
    assert caught.value.code == "lease-metadata-binding-mismatch"
    assert caught.value.details["mismatched"]["proposal_sha256"] == {
        "expected": preview["proposal_sha256"],
        "observed": "0" * 64,
    }




def test_publication_writes_receipt_and_is_idempotent(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    receipt = tmp_path / "receipt.json"
    first = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
    )
    second = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
    )
    assert first["status"] == "published"
    assert first["queue_mutated"] is False
    assert first["task_spec_revision"]["revision"] == 1
    plan = json.loads(plan_path.read_text())
    assert first["task_spec_revision"]["spec_sha256"] == plan["task_json_sha256"]
    stored = store.task_spec(first["task_id"])
    assert stored is not None
    assert stored["spec_sha256"] == plan["task_json_sha256"]
    assert store.replay_projection()["task_specs"]["matches_current"] is True
    assert second["idempotent_replay"] is True


def test_publication_receipt_replay_survives_later_registry_drift(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    receipt = tmp_path / "receipt.json"
    first = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
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
    )
    assert replay["receipt_sha256"] == first["receipt_sha256"]
    assert replay["idempotent_replay"] is True


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
        )
    assert caught.value.code == "receipt-integrity-invalid"




def test_publishing_task_git_projection_drift_does_not_block_state_store_publication(
    registry_factory, tmp_path
):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    publishing_task_path = root / "registry" / "tasks" / "BUR-TEST-001-T001.json"
    publishing_task = json.loads(publishing_task_path.read_text())
    publishing_task["title"] = "Compatibility projection changed after review"
    publishing_task_path.write_text(json.dumps(publishing_task, indent=2) + "\n")
    _git(root, "add", str(publishing_task_path.relative_to(root)))
    _git(root, "commit", "-m", "change compatibility projection")
    drifted_registry = Registry.load(root)

    result = publish_task_proposal(
        drifted_registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "unused-workspace",
        receipt_path=tmp_path / "receipt.json",
    )

    assert result["status"] == "published"
    assert result["publication_mode"] == "state_store"
    assert result["queue_mutated"] is False
    stored = store.task_spec(result["task_id"])
    assert stored is not None
    assert stored["spec_sha256"] == result["task_spec_revision"]["spec_sha256"]

def test_publication_receipt_write_failure_reports_state_store_readback(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    blocked_parent = tmp_path / "receipt-parent-is-file"
    blocked_parent.write_text("not a directory")
    receipt = blocked_parent / "receipt.json"

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "unused-workspace",
            receipt_path=receipt,
        )

    assert caught.value.code == "receipt-write-unclear"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    task_id = json.loads(plan_path.read_text())["task_id"]
    assert f"StateStore TaskSpec {task_id}" in caught.value.required_readback
    stored = store.task_spec(json.loads(plan_path.read_text())["task_id"])
    assert stored is not None


def test_publication_recovers_exact_register_commit_after_pre_receipt_failure(
    registry_factory, tmp_path, monkeypatch
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    receipt = tmp_path / "receipt.json"
    original_replay_projection = store.replay_projection
    failures_remaining = 2

    def replay_projection_with_two_failures():
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise RuntimeError("injected projection failure before publication receipt")
        return original_replay_projection()

    monkeypatch.setattr(store, "replay_projection", replay_projection_with_two_failures)
    plan = json.loads(plan_path.read_text())
    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=_lease_db(preview, tmp_path),
            workspace_root=tmp_path / "workspaces",
            receipt_path=receipt,
        )

    assert caught.value.code == "task-spec-projection-postcommit-failed"
    assert caught.value.effect_started is True
    assert caught.value.ambiguity is True
    assert caught.value.retryable is False
    assert caught.value.publication_phase == "committed_locally"
    assert f"StateStore TaskSpec {plan['task_id']}" in caught.value.required_readback
    assert "StateStore projection replay" in caught.value.required_readback
    assert "publication lease rows" in caught.value.required_readback
    committed = store.task_spec(plan["task_id"])
    assert committed is not None
    assert committed["revision"] == 1
    assert committed["spec_sha256"] == plan["task_json_sha256"]
    assert not receipt.exists()

    retry_preview = publication_preview(registry, store, plan_path=plan_path)
    retry_resource_db = _lease_db(retry_preview, tmp_path)
    with pytest.raises(OperatorIntakeError) as replay_caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding=_lease_binding(),
            resource_db=retry_resource_db,
            workspace_root=tmp_path / "workspaces-retry-failed",
            receipt_path=receipt,
        )

    assert replay_caught.value.code == "task-spec-projection-postcommit-failed"
    assert replay_caught.value.effect_started is False
    assert replay_caught.value.ambiguity is True
    assert replay_caught.value.retryable is False
    assert replay_caught.value.publication_phase == "committed_locally"
    assert replay_caught.value.details["task_spec_revision"]["idempotent_replay"] is True
    assert store.task_spec(plan["task_id"])["revision"] == 1
    assert not receipt.exists()

    recovered = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=retry_resource_db,
        workspace_root=tmp_path / "workspaces-retry",
        receipt_path=receipt,
    )

    assert recovered["status"] == "published"
    assert recovered["task_spec_revision"]["revision"] == 1
    assert recovered["task_spec_revision"]["idempotent_replay"] is True
    assert store.task_spec(plan["task_id"])["revision"] == 1
    assert receipt.is_file()


def test_publication_preview_rejects_identical_register_from_foreign_mutation(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    _review(plan_path)
    plan = json.loads(plan_path.read_text())
    foreign = store.put_task_spec(
        plan["task_json"],
        idempotency_key="foreign-register",
        expected_revision=None,
        source="test-foreign-register",
    )
    assert foreign["revision"] == 1
    assert foreign["spec_sha256"] == plan["task_json_sha256"]

    with pytest.raises(OperatorIntakeError) as caught:
        publication_preview(registry, store, plan_path=plan_path)

    assert caught.value.code == "task-spec-register-replay-mutation-mismatch"


def _cli_result(capsys):
    payload = json.loads(capsys.readouterr().out)
    return payload.get("result", payload)


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

    refinement_path = tmp_path / "candidate-refinement.json"
    refinement_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "idempotency_key": "cli:operator-intake-refinement",
                "title": "Refined CLI candidate adapter",
                "source_kind": "test-fixture",
                "source_locator": "fixture:cli-refinement",
                "source_sha256": "d" * 64,
                "desired_outcome": "Prove source-bound candidate refinement",
                "supersedes_event_id": recorded["event_id"],
            }
        )
    )
    assert (
        bureau_cli.main(
            [
                *common,
                "operator-candidate-record",
                "--request",
                str(refinement_path),
            ]
        )
        == 0
    )
    refined = _cli_result(capsys)
    assert refined["candidate_id"] == recorded["candidate_id"]
    assert refined["record"]["supersedes_event_id"] == recorded["event_id"]
    recorded = refined

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

    assert (
        bureau_cli.main(
            [
                *common,
                "operator-candidate-assess",
                "--idempotency-key",
                "cli:operator-intake-refinement",
            ]
        )
        == 0
    )
    assessed_by_key = _cli_result(capsys)
    assert assessed_by_key["kind"] == "bureau_candidate_assessment"
    assert assessed_by_key["candidate_id"] == recorded["candidate_id"]
    assert assessed_by_key["event_id"] == recorded["event_id"]

    StateStore(state_db).import_registry_task_specs(Registry.load(root))
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

    assert (
        bureau_cli.main(
            [
                *common,
                "operator-task-review",
                "--plan",
                str(plan_path),
                "--reviewer",
                "ChatGPT through Grabowski",
                "--proposal-sha256",
                proposed["proposal_sha256"],
            ]
        )
        == 0
    )
    reviewed = _cli_result(capsys)
    assert reviewed["kind"] == "bureau_task_review_result"
    assert reviewed["status"] == "reviewed"
    assert reviewed["approval"]["allowed"] is True

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


def test_task_propose_rejects_broad_bureau_scope_before_plan_write(registry_factory, tmp_path):
    _root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    recorded = _record(registry, store, key="source:broad-bureau-scope")
    task = _task(registry.root, "BUR-TEST-001-T099")
    task["execution"]["grabowski_resources"] = ["repo:/home/alex/repos/bureau"]
    plan_path = tmp_path / "broad-proposal.json"

    with pytest.raises(OperatorIntakeError) as error:
        task_propose(
            registry,
            store,
            task_json=task,
            publishing_task_id="BUR-TEST-001-T001",
            path=plan_path,
            candidate_id=recorded["candidate_id"],
        )

    assert error.value.code == "broad-bureau-task-scope-forbidden"
    assert error.value.details["scope_assessment"]["exception_status"] == "missing"
    assert not plan_path.exists()


def _t026_foreign_open_pr(plan: dict, *, number: int = 1112) -> dict:
    return {
        "number": number,
        "head_sha": "d" * 40,
        "head_ref_name": "foreign/operator-ecosystem-redundancy-v1-t065",
        "head_repository_id": 2282253578,
        "head_repository": "foreign/bureau",
        "base_sha": plan["registry"]["commit"],
        "base_ref_name": "main",
        "task_paths": [plan["target_path"]],
        "task_ids": [plan["task_id"]],
        "tasks": [],
    }










def test_github_repository_for_preview_prefers_git_origin(
    registry_factory, monkeypatch
):
    root, _ = _committed_registry(registry_factory)
    _git(root, "remote", "add", "origin", "git@github.com:example/bureau.git")

    def unexpected_runtime_identity(_root):
        raise AssertionError("runtime snapshot fallback must not run when origin is valid")

    monkeypatch.setattr(
        operator_intake_module,
        "bureau_runtime_identity",
        unexpected_runtime_identity,
    )

    assert operator_intake_module._github_repository_for_preview(root) == "example/bureau"


def test_github_repository_for_preview_accepts_manifest_bound_runtime_snapshot(
    registry_factory, tmp_path, monkeypatch
):
    root, _ = _committed_registry(registry_factory)
    _, snapshot = _runtime_snapshot_registry(root, tmp_path, monkeypatch)

    assert (
        operator_intake_module._github_repository_for_preview(snapshot)
        == "heimgewebe/bureau"
    )


def test_github_repository_for_preview_rejects_arbitrary_non_git_root(tmp_path):
    root = tmp_path / "plain-registry"
    root.mkdir()

    assert operator_intake_module._github_repository_for_preview(root) is None


def _merged_task_promotion_fixture(registry_factory, tmp_path, monkeypatch):
    root, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    recorded = _record(registry, store, key="source:promotion")
    task = _task(root)
    task["depends_on"] = []
    plan_path = tmp_path / "promotion-proposal.json"
    task_propose(
        registry,
        store,
        candidate_id=recorded["candidate_id"],
        task_json=task,
        publishing_task_id="BUR-TEST-001-T001",
        path=plan_path,
    )
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    publication_receipt = tmp_path / "publication-receipt.json"
    published = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=publication_receipt,
    )
    task_path = root / published["target_path"]
    task_path.parent.mkdir(parents=True, exist_ok=True)
    exact_plan = json.loads(plan_path.read_text())
    task_path.write_bytes(operator_intake_module._render_task(exact_plan["task_json"]))
    _git(root, "add", published["target_path"])
    _git(root, "commit", "-m", "merge published standalone task fixture")
    merged_registry = Registry.load(root)
    monkeypatch.setattr(
        operator_intake_module,
        "_github_repository_for_preview",
        lambda _root: "example/bureau",
    )
    monkeypatch.setattr(
        operator_intake_module,
        "_promotion_pull_request_readback",
        lambda repository, number: {
            "number": number,
            "state": "MERGED",
            "mergedAt": "2026-08-16T15:00:00Z",
            "mergeCommit": {"oid": "a" * 40},
            "headRefOid": published["publication"]["head"],
            "headRefName": published["publication"]["branch"],
            "baseRefName": "main",
            "url": f"https://example.invalid/pull/{number}",
        },
    )
    return root, merged_registry, store, publication_receipt, published
















def test_operator_task_ready_effect_scope_is_read_only_except_apply():
    parser = bureau_cli.parser()
    preview = parser.parse_args(
        ["operator-task-ready", "--publication-receipt", "pub.json", "--preview"]
    )
    readback = parser.parse_args(
        [
            "operator-task-ready",
            "--publication-receipt",
            "pub.json",
            "--readback",
            "--promotion-receipt",
            "promotion.json",
        ]
    )
    apply = parser.parse_args(
        [
            "operator-task-ready",
            "--publication-receipt",
            "pub.json",
            "--apply",
            "--promotion-receipt",
            "promotion.json",
        ]
    )
    assert bureau_cli._command_effect_scope(preview) == "read_only"
    assert bureau_cli._command_effect_scope(readback) == "read_only"
    assert bureau_cli._command_effect_scope(apply) == "coordination_state_mutation"


def _state_store_publication_fixture(registry_factory, tmp_path):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = json.loads(plan_path.read_text())
    plan["task_json"]["depends_on"] = []
    plan["task_json_sha256"] = operator_intake_module.legacy.sha256_json(plan["task_json"])
    rendered = operator_intake_module._render_task(plan["task_json"])
    plan["task_file_sha256"] = hashlib.sha256(rendered).hexdigest()
    plan["task_spec"]["proposed_spec_sha256"] = plan["task_json_sha256"]
    plan["proposed_diff_sha256"] = operator_intake_module._task_change_sha256(
        plan["target_path"],
        rendered,
        before_sha256=plan["task_spec"]["expected_task_file_sha256"],
    )
    unsigned = {
        key: value
        for key, value in plan.items()
        if key not in {"proposal_sha256", "review"}
    }
    plan["proposal_sha256"] = operator_intake_module.legacy.sha256_json(unsigned)
    plan["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "operator-self-review",
        "reviewed_at": "2026-08-18T17:00:00Z",
        "reviewed_proposal_sha256": plan["proposal_sha256"],
    }
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    preview = publication_preview(registry, store, plan_path=plan_path)
    receipt_path = tmp_path / "state-publication-receipt.json"
    published = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "unused-workspace",
        receipt_path=receipt_path,
    )
    return registry, store, receipt_path, published


def test_state_store_publication_does_not_materialize_git_task_or_queue(registry_factory, tmp_path):
    root, registry = _committed_registry(registry_factory)
    queue_before = (root / "registry/queue.json").read_bytes()
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    task_id = json.loads(plan_path.read_text())["task_id"]
    target = root / f"registry/tasks/{task_id}.json"
    assert not target.exists()
    _review(plan_path)
    preview = publication_preview(registry, store, plan_path=plan_path)
    result = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "unused-workspace",
        receipt_path=tmp_path / "receipt.json",
    )
    assert result["publication_mode"] == "state_store"
    assert not target.exists()
    assert (root / "registry/queue.json").read_bytes() == queue_before
    assert store.task_spec(task_id) is not None


def test_state_store_task_ready_preview_apply_readback_and_replay(registry_factory, tmp_path):
    registry, store, receipt_path, published = _state_store_publication_fixture(
        registry_factory, tmp_path
    )
    promotion_receipt = tmp_path / "promotion-receipt.json"
    preview = operator_intake_module.promote_task_ready(
        registry,
        store,
        publication_receipt_path=receipt_path,
        mode="preview",
    )
    assert preview["status"] == "ready"
    assert preview["publication"]["mode"] == "state_store"
    applied = operator_intake_module.promote_task_ready(
        registry,
        store,
        publication_receipt_path=receipt_path,
        promotion_receipt_path=promotion_receipt,
        mode="apply",
    )
    assert applied["status"] == "promoted"
    assert store.task_spec(published["task_id"])["spec"]["state"] == "ready"
    readback = operator_intake_module.promote_task_ready(
        registry,
        store,
        publication_receipt_path=receipt_path,
        promotion_receipt_path=promotion_receipt,
        mode="readback",
    )
    assert readback["status"] == "promoted"
    replay = operator_intake_module.promote_task_ready(
        registry,
        store,
        publication_receipt_path=receipt_path,
        promotion_receipt_path=promotion_receipt,
        mode="apply",
    )
    assert replay["idempotent_replay"] is True


def test_operator_publication_commands_are_coordination_state_mutations():
    from bureau.effect_scope import classify_command_effect_scope

    assert (
        classify_command_effect_scope("operator-task-publish", mutates=True)
        == "coordination_state_mutation"
    )
    assert (
        classify_command_effect_scope("operator-task-ready", mutates=True)
        == "coordination_state_mutation"
    )


def _identity_revision_task(
    *,
    title: str,
    resource: str,
    initiative: str = "INIT-V1",
    goal: str = "",
    mode: str = "write",
    acceptance_ids: tuple[str, ...] = (),
) -> dict:
    return {
        "id": "INIT-V1-T001",
        "initiative": initiative,
        "title": title,
        "goal": goal,
        "claims": [{"resource": resource, "mode": mode, "isolation": "worktree"}],
        "acceptance": [{"id": item_id} for item_id in acceptance_ids],
    }


def test_task_revision_identity_guard_rejects_disjoint_scope_and_subject() -> None:
    before = _identity_revision_task(
        title="Reposkop runtime interpreter cutover", resource="component.grabowski.runtime"
    )
    after = _identity_revision_task(
        title="Metarepo compatibility surfaces cleanup", resource="repo.metarepo"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_allows_disjoint_scope_with_title_continuity() -> None:
    before = _identity_revision_task(
        title="Define Chronik high-value operator events", resource="repo.chronik"
    )
    after = _identity_revision_task(
        title="Filter Chronik operator events to high-value signals", resource="repo.grabowski"
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_rejects_shared_suffix_after_action() -> None:
    before = _identity_revision_task(
        title="Repair Avira updater service", resource="repo.infra"
    )
    after = _identity_revision_task(
        title="Repair database updater service", resource="repo.infra"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_shared_suffix_after_unlisted_action() -> None:
    before = _identity_revision_task(
        title="Upgrade Avira updater service", resource="repo.infra"
    )
    after = _identity_revision_task(
        title="Upgrade database updater service", resource="repo.infra"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_hyphenated_process_prefix_subject_swap() -> None:
    before = _identity_revision_task(
        title="Pre-check Avira updater service", resource="repo.infra"
    )
    after = _identity_revision_task(
        title="Pre-check database updater service", resource="repo.infra"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_allows_identifier_leading_subject_rewrite() -> None:
    before = _identity_revision_task(
        title="Bureau-Run-Leases revisionsgebunden an Captain delegieren",
        resource="repo.grabowski",
        goal="Bind lease delegation to the current revision",
    )
    after = _identity_revision_task(
        title="Bureau-Run-Leases sicher an Captain delegieren",
        resource="repo.grabowski",
        goal="Keep delegation safe across revision changes",
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_allows_regular_plural_inflection() -> None:
    before = _identity_revision_task(
        title="Improve database migrations", resource="repo.database"
    )
    after = _identity_revision_task(
        title="Improve database migration", resource="repo.database"
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_allows_single_subject_plural_inflection() -> None:
    before = _identity_revision_task(
        title="Improve migrations", resource="repo.database"
    )
    after = _identity_revision_task(
        title="Improve migration", resource="repo.database"
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_does_not_stem_distinct_identifier() -> None:
    before = _identity_revision_task(
        title="Improve Canva exports", resource="repo.design"
    )
    after = _identity_revision_task(
        title="Improve canvas exports", resource="repo.design"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_does_not_conflate_news_with_new() -> None:
    before = _identity_revision_task(
        title="Improve news pipeline", resource="repo.media", goal="Refresh editorial feeds"
    )
    after = _identity_revision_task(
        title="Improve new pipeline", resource="repo.media", goal="Replace deployment flow"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_same_scope_unrelated_subject() -> None:
    before = _identity_revision_task(
        title="Repair Avira updater", resource="repo.infra", goal="Restore updater health"
    )
    after = _identity_revision_task(
        title="Tune laptop thermal firmware",
        resource="repo.infra",
        goal="Restore thermal headroom and firmware baseline",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_allows_goal_continuity_after_title_rewrite() -> None:
    before = _identity_revision_task(
        title="Old implementation wording",
        resource="repo.alpha",
        goal="Preserve exact runtime identity",
    )
    after = _identity_revision_task(
        title="Completely renamed delivery slice",
        resource="repo.beta",
        goal="Preserve exact runtime identity while revising delivery",
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_allows_write_to_read_revalidation() -> None:
    before = _identity_revision_task(
        title="Build persistent recall store",
        resource="repo.grabowski",
        goal="Implement recall persistence",
    )
    after = _identity_revision_task(
        title="Revalidate recall store against current truth architecture",
        resource="repo.grabowski",
        goal="Decide whether any separate recall store is still needed",
        mode="read",
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_rejects_generic_action_token_only() -> None:
    before = _identity_revision_task(
        title="Repair Avira updater", resource="repo.infra"
    )
    after = _identity_revision_task(
        title="Repair database", resource="repo.database"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_two_generic_tokens() -> None:
    before = _identity_revision_task(
        title="Create new backup", resource="repo.backup"
    )
    after = _identity_revision_task(
        title="Create new dashboard", resource="repo.dashboard"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_one_weak_shared_subject_token() -> None:
    before = _identity_revision_task(
        title="Create useful backup", resource="repo.backup"
    )
    after = _identity_revision_task(
        title="Create useful dashboard", resource="repo.dashboard"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_two_weak_tokens_across_resources() -> None:
    before = _identity_revision_task(
        title="Create secure reliable backup", resource="repo.backup"
    )
    after = _identity_revision_task(
        title="Create secure reliable dashboard", resource="repo.dashboard"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_allows_single_complete_subject_token() -> None:
    before = _identity_revision_task(
        title="Create backup",
        resource="repo.backup",
        acceptance_ids=("backup-contract",),
    )
    after = _identity_revision_task(
        title="Migrate backup",
        resource="repo.backup",
        acceptance_ids=("backup-contract",),
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_rejects_single_subject_across_resources() -> None:
    before = _identity_revision_task(
        title="Create backup", resource="repo.backup"
    )
    after = _identity_revision_task(
        title="Migrate backup", resource="repo.archive"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_allows_exact_one_token_title() -> None:
    before = _identity_revision_task(
        title="contracts",
        resource="repo.heimlern",
        acceptance_ids=("contracts",),
    )
    after = _identity_revision_task(
        title="contracts",
        resource="repo.heimlern",
        acceptance_ids=("contracts",),
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_allows_retained_typed_acceptance_anchor() -> None:
    before = _identity_revision_task(
        title="Signed one call replay filter",
        resource="repo.grabowski",
        goal="Decouple read only replay handling",
        acceptance_ids=("replay-proof", "transport-proof"),
    )
    after = _identity_revision_task(
        title="READ_ONLY transport proof",
        resource="repo.grabowski",
        goal="Prove the transport contract before cutover",
        acceptance_ids=("replay-proof", "transport-proof"),
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_rejects_reused_acceptance_ids_with_new_contract() -> None:
    before = _identity_revision_task(
        title="Backup retention archive",
        resource="repo.shared",
        goal="Restore archived snapshots",
    )
    after = _identity_revision_task(
        title="Customer invoice dashboard",
        resource="repo.shared",
        goal="Render billing balances",
    )
    shared_contract = {
        "evidence_type": "object",
        "verifier": "manual_observation",
        "verifier_config": {"observation_scope": "shared-contract"},
    }
    before["acceptance"] = [
        {
            "id": "proof-a",
            "assertion": "Backup restore completes from an archived snapshot",
            **shared_contract,
        },
        {
            "id": "proof-b",
            "assertion": "Retention policy keeps backup copies for seven days",
            **shared_contract,
        },
    ]
    after["acceptance"] = [
        {
            "id": "proof-a",
            "assertion": "Customer dashboard renders current billing balances",
            **shared_contract,
        },
        {
            "id": "proof-b",
            "assertion": "Invoice export lists settled customer payments",
            **shared_contract,
        },
    ]
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_reused_acceptance_ids_with_suffix_only_assertions(
) -> None:
    before = _identity_revision_task(
        title="Backup retention archive",
        resource="repo.shared",
        goal="Restore archived snapshots",
    )
    after = _identity_revision_task(
        title="Customer invoice dashboard",
        resource="repo.shared",
        goal="Render billing balances",
    )
    shared_contract = {
        "evidence_type": "object",
        "verifier": "manual_observation",
        "verifier_config": {"observation_scope": "shared-contract"},
    }
    before["acceptance"] = [
        {
            "id": item_id,
            "assertion": assertion,
            **shared_contract,
        }
        for item_id, assertion in (
            ("proof-a", "Verify backup retention policy state"),
            ("proof-b", "Verify backup retention archive state"),
        )
    ]
    after["acceptance"] = [
        {
            **criterion,
            "assertion": criterion["assertion"].replace("backup", "billing"),
        }
        for criterion in before["acceptance"]
    ]
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_reused_acceptance_ids_with_changed_verifier_contract(
) -> None:
    before = _identity_revision_task(
        title="Backup retention archive",
        resource="repo.shared",
        goal="Restore archived snapshots",
    )
    after = _identity_revision_task(
        title="Customer invoice dashboard",
        resource="repo.shared",
        goal="Render billing balances",
    )
    before["acceptance"] = [
        {
            "id": item_id,
            "assertion": assertion,
            "evidence_type": "object",
            "verifier": "manual_observation",
            "verifier_config": {"observation_scope": "backup"},
        }
        for item_id, assertion in (
            ("proof-a", "Backup restore completes from an archived snapshot"),
            ("proof-b", "Retention policy keeps backup copies for seven days"),
        )
    ]
    after["acceptance"] = [
        {
            **criterion,
            "verifier_config": {"observation_scope": "billing"},
        }
        for criterion in before["acceptance"]
    ]
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_unanchored_exact_one_token_title() -> None:
    before = _identity_revision_task(
        title="Upgrade",
        resource="repo.shared",
        goal="Preserve backup retention",
        acceptance_ids=("backup-contract",),
    )
    after = _identity_revision_task(
        title="Upgrade",
        resource="repo.shared",
        goal="Render customer dashboard",
        acceptance_ids=("dashboard-contract",),
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_treats_unicode_words_as_single_tokens() -> None:
    before = _identity_revision_task(
        title="Prüfe Avira Updater", resource="repo.infra"
    )
    after = _identity_revision_task(
        title="Prüfe Datenbank", resource="repo.database"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_allows_unrelated_write_to_read_closeout() -> None:
    before = _identity_revision_task(
        title="Build backup retention", resource="repo.shared"
    )
    after = _identity_revision_task(
        title="Inspect dashboard metrics", resource="repo.shared", mode="read"
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_rejects_unlisted_leading_process_word() -> None:
    before = _identity_revision_task(
        title="Upgrade legacy backup", resource="repo.backup"
    )
    after = _identity_revision_task(
        title="Upgrade legacy dashboard", resource="repo.dashboard"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_preserves_leading_noun_on_resource_change() -> None:
    before = _identity_revision_task(
        title="Backup retention policy", resource="repo.backup"
    )
    after = _identity_revision_task(
        title="Dashboard retention policy", resource="repo.dashboard"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_checks_full_tokens_on_retained_write_scope() -> None:
    before = _identity_revision_task(
        title="Avira updater service", resource="repo.shared"
    )
    after = _identity_revision_task(
        title="Database updater service", resource="repo.shared"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_ignores_incidental_read_claim_overlap() -> None:
    before = _identity_revision_task(title="Repair tests", resource="repo.alpha")
    before["claims"].append(
        {"resource": "repo.shared", "mode": "read", "isolation": "worktree"}
    )
    after = _identity_revision_task(title="Build tests", resource="repo.beta")
    after["claims"].append(
        {"resource": "repo.shared", "mode": "read", "isolation": "worktree"}
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_preserves_scoped_and_hyphenated_identifiers() -> None:
    before = _identity_revision_task(
        title="Repair @scope/pkg release", resource="repo.package"
    )
    after = _identity_revision_task(
        title="Repair scope-pkg release", resource="repo.other"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_exact_generic_text() -> None:
    before = _identity_revision_task(
        title="Update",
        resource="repo.backup",
        goal="Preserve backup retention",
    )
    after = _identity_revision_task(
        title="Update",
        resource="repo.dashboard",
        goal="Render customer dashboard",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_preserves_identifier_punctuation() -> None:
    before = _identity_revision_task(
        title="Improve C++ compiler", resource="repo.cpp"
    )
    after = _identity_revision_task(
        title="Repair C# compiler", resource="repo.csharp"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_unrelated_read_to_write() -> None:
    before = _identity_revision_task(
        title="Inspect backup reports", resource="repo.backup", mode="read"
    )
    after = _identity_revision_task(
        title="Repair database", resource="repo.database"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_allows_related_read_to_write() -> None:
    before = _identity_revision_task(
        title="Inspect database corruption", resource="repo.database", mode="read"
    )
    after = _identity_revision_task(
        title="Repair database corruption", resource="repo.database"
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


def test_task_revision_identity_guard_rejects_initiative_rebind() -> None:
    before = _identity_revision_task(title="Keep the same task", resource="repo.alpha")
    after = _identity_revision_task(
        title="Keep the same task", resource="repo.alpha", initiative="OTHER-V1"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-initiative-mismatch"


def test_task_propose_reactivation_uses_initial_identity(
    registry_factory, tmp_path
) -> None:
    root = registry_factory(task_count=2, mode="write")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    original = store.task_spec(task_id)
    assert original is not None

    closeout = json.loads(json.dumps(original["spec"]))
    closeout["title"] = "Inspect unrelated dashboard metrics"
    closeout["goal"] = "Read a different dashboard subject"
    closeout["claims"][0]["mode"] = "read"
    closeout["acceptance"][0]["id"] = "dashboard-proof"
    with store.connect() as connection:
        task_specs_module.put(
            connection,
            closeout,
            idempotency_key="test:read-closeout",
            expected_revision=int(original["revision"]),
            source="test",
        )
    read_current = store.task_spec(task_id)
    assert read_current is not None
    assert int(read_current["revision"]) == int(original["revision"]) + 1
    identity_baseline = operator_intake_module._task_revision_identity_baseline(
        store, read_current
    )
    assert identity_baseline["revision"] == original["revision"]

    reactivation = json.loads(json.dumps(closeout))
    reactivation["claims"][0]["mode"] = "write"
    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:laundered-read-reactivation",
        title="Attempt laundered read reactivation",
        source_kind="runtime-diagnostic",
        source_locator="bureau:laundered-read-reactivation",
        source_sha256="8" * 64,
        desired_outcome="Reject an unrelated write reactivation",
        repo="repo.beta",
        task_id=task_id,
    )
    with pytest.raises(OperatorIntakeError) as raised:
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=reactivation,
            publishing_task_id="BUR-TEST-001-T001",
            path=tmp_path / "laundered-read-reactivation.proposal.json",
        )
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_propose_first_write_after_read_drift_uses_initial_identity(
    registry_factory, tmp_path
) -> None:
    root = registry_factory(task_count=2, mode="read")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    original = store.task_spec(task_id)
    assert original is not None

    drifted_read = json.loads(json.dumps(original["spec"]))
    drifted_read["title"] = "Inspect unrelated dashboard metrics"
    drifted_read["acceptance"][0]["id"] = "dashboard-proof"
    with store.connect() as connection:
        task_specs_module.put(
            connection,
            drifted_read,
            idempotency_key="test:read-only-identity-drift",
            expected_revision=int(original["revision"]),
            source="test",
        )
    current = store.task_spec(task_id)
    assert current is not None
    proposed = json.loads(json.dumps(current["spec"]))
    proposed["claims"][0]["mode"] = "write"
    operator_intake_module._validate_task_revision_identity_continuity(
        current["spec"], proposed
    )
    identity_baseline = operator_intake_module._task_revision_identity_baseline(
        store, current
    )
    assert identity_baseline["revision"] == original["revision"]

    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:first-write-after-read-drift",
        title="Attempt first write after read drift",
        source_kind="runtime-diagnostic",
        source_locator="bureau:first-write-after-read-drift",
        source_sha256="a" * 64,
        desired_outcome="Reject write activation after unrelated read-only identity drift",
        repo="repo.beta",
        task_id=task_id,
    )
    with pytest.raises(OperatorIntakeError) as raised:
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=proposed,
            publishing_task_id="BUR-TEST-001-T001",
            path=tmp_path / "first-write-after-read-drift.proposal.json",
        )
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_propose_write_chain_uses_first_write_identity(
    registry_factory, tmp_path
) -> None:
    root = registry_factory(task_count=2, mode="write")
    task_path = root / "registry/tasks/BUR-TEST-001-T002.json"
    task = json.loads(task_path.read_text())
    task["title"] = "Repair Avira updater service"
    task_path.write_text(json.dumps(task))
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    original = store.task_spec(task_id)
    assert original is not None

    intermediate = json.loads(json.dumps(original["spec"]))
    intermediate["title"] = "Repair updater service database"
    with store.connect() as connection:
        task_specs_module.put(
            connection,
            intermediate,
            idempotency_key="test:gradual-write-drift-intermediate",
            expected_revision=int(original["revision"]),
            source="test",
        )
    current = store.task_spec(task_id)
    assert current is not None
    assert int(current["revision"]) == int(original["revision"]) + 1

    proposed = json.loads(json.dumps(current["spec"]))
    proposed["title"] = "Repair service database dashboard"
    # The immediate predecessor still looks locally continuous. The stable baseline
    # must be what prevents gradual laundering of the permanent task identity.
    operator_intake_module._validate_task_revision_identity_continuity(
        current["spec"], proposed
    )
    identity_baseline = operator_intake_module._task_revision_identity_baseline(
        store, current
    )
    assert identity_baseline["revision"] == original["revision"]

    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:gradual-write-drift",
        title="Attempt gradual write identity drift",
        source_kind="runtime-diagnostic",
        source_locator="bureau:gradual-write-drift",
        source_sha256="9" * 64,
        desired_outcome="Reject gradual semantic drift under one permanent task id",
        repo="repo.beta",
        task_id=task_id,
    )
    with pytest.raises(OperatorIntakeError) as raised:
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=proposed,
            publishing_task_id="BUR-TEST-001-T001",
            path=tmp_path / "gradual-write-drift.proposal.json",
        )
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_reviewed_pre_hardening_revision_cannot_bypass_publication_guard(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(task_count=2, mode="write")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    current = store.task_spec(task_id)
    assert current is not None
    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:pre-hardening-semantic-reuse",
        title="Attempt pre-hardening unrelated reuse",
        source_kind="runtime-diagnostic",
        source_locator="bureau:pre-hardening-semantic-reuse",
        source_sha256="7" * 64,
        desired_outcome="Reuse an existing id for unrelated write work",
        repo="repo.alpha",
        task_id=task_id,
    )
    revised = json.loads(json.dumps(current["spec"]))
    revised["title"] = "Completely unrelated replacement subject"
    revised["goal"] = "Perform unrelated write work under the existing identifier."
    with monkeypatch.context() as proposal_context:
        proposal_context.setattr(
            operator_intake_module,
            "_validate_task_revision_identity_continuity",
            lambda *_: None,
        )
        plan_path = tmp_path / "pre-hardening-semantic-reuse.proposal.json"
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=revised,
            publishing_task_id="BUR-TEST-001-T001",
            path=plan_path,
        )
    pending = json.loads(plan_path.read_text())
    review_task_proposal(
        plan_path=plan_path,
        reviewer="post-hardening-reviewer",
        expected_proposal_sha256=pending["proposal_sha256"],
    )
    before = store.task_spec(task_id)
    with pytest.raises(OperatorIntakeError) as raised:
        publication_preview(registry, store, plan_path=plan_path)
    assert raised.value.code == "task-revision-identity-discontinuity"
    assert store.task_spec(task_id) == before


def test_task_propose_rejects_malformed_revision_claims_as_schema_error(
    registry_factory, tmp_path
) -> None:
    root = registry_factory(task_count=2, mode="write")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    current = store.task_spec(task_id)
    assert current is not None
    recorded = candidate_record(
        registry,
        store,
        idempotency_key="source:malformed-revision-claims",
        title="Malformed revision claims",
        source_kind="runtime-diagnostic",
        source_locator="bureau:malformed-revision-claims",
        source_sha256="6" * 64,
        desired_outcome="Reject malformed revision claims without a traceback",
        repo="repo.alpha",
        task_id=task_id,
    )
    revised = json.loads(json.dumps(current["spec"]))
    revised["claims"] = None
    plan_path = tmp_path / "malformed-revision-claims.proposal.json"
    with pytest.raises(OperatorIntakeError) as raised:
        task_propose(
            registry,
            store,
            candidate_id=recorded["candidate_id"],
            task_json=revised,
            publishing_task_id="BUR-TEST-001-T001",
            path=plan_path,
        )
    assert raised.value.code == "task-schema-invalid"
    assert not plan_path.exists()


def test_task_propose_rejects_reuse_of_existing_id_for_unrelated_write_scope(
    registry_factory, tmp_path
) -> None:
    root = registry_factory(task_count=2, mode="write")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    store.import_registry_task_specs(registry)
    task_id = "BUR-TEST-001-T002"
    current = store.task_spec(task_id)
    assert current is not None
    current_resources = {claim["resource"] for claim in current["spec"]["claims"]}
    replacement = next(
        resource for resource in registry.resources if resource not in current_resources
    )
    recorded = candidate_record(
        registry, store, idempotency_key="source:semantic-reuse",
        title="Attempt unrelated reuse", source_kind="runtime-diagnostic",
        source_locator="bureau:semantic-reuse", source_sha256="8" * 64,
        desired_outcome="Reuse an existing id for unrelated work",
        repo="repo.alpha", task_id=task_id,
    )
    revised = json.loads(json.dumps(current["spec"]))
    revised["title"] = "Unrelated replacement work with another subject"
    revised["goal"] = "Perform a materially different task under the old identifier."
    revised["claims"] = [{"resource": replacement, "mode": "write", "isolation": "worktree"}]
    plan_path = tmp_path / "semantic-reuse.proposal.json"
    with pytest.raises(OperatorIntakeError) as raised:
        task_propose(
            registry, store, candidate_id=recorded["candidate_id"], task_json=revised,
            publishing_task_id="BUR-TEST-001-T001", path=plan_path,
        )
    assert raised.value.code == "task-revision-identity-discontinuity"
    assert not plan_path.exists()



def test_task_revision_identity_guard_rejects_acceptance_predicate_inversion_anchor() -> None:
    before = _identity_revision_task(
        title="Backup retention archive",
        resource="repo.shared",
        goal="Restore archived snapshots",
    )
    after = _identity_revision_task(
        title="Customer invoice dashboard",
        resource="repo.shared",
        goal="Render billing balances",
    )
    shared_contract = {
        "evidence_type": "object",
        "verifier": "manual_observation",
        "verifier_config": {"observation_scope": "shared-contract"},
    }
    before["acceptance"] = [
        {
            "id": "proof-a",
            "assertion": "Verify backup policy prevents customer data deletion in production",
            **shared_contract,
        },
        {
            "id": "proof-b",
            "assertion": "Verify archive policy prevents snapshot deletion in production",
            **shared_contract,
        },
    ]
    after["acceptance"] = [
        {
            **criterion,
            "assertion": criterion["assertion"].replace("prevents", "permits"),
        }
        for criterion in before["acceptance"]
    ]
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_removed_strong_leading_identifier() -> None:
    before = _identity_revision_task(
        title="API backup updater service", resource="repo.infra"
    )
    after = _identity_revision_task(
        title="Repair dashboard updater service", resource="repo.infra"
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_allows_identifier_moved_behind_action() -> None:
    before = _identity_revision_task(
        title="API runtime cleanup",
        resource="repo.infra",
        acceptance_ids=("api-runtime",),
    )
    after = _identity_revision_task(
        title="Clean API runtime",
        resource="repo.infra",
        acceptance_ids=("api-runtime",),
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)


@pytest.mark.parametrize(
    ("plural", "singular"),
    [
        ("backups", "backup"),
        ("contracts", "contract"),
        ("tasks", "task"),
        ("files", "file"),
        ("workers", "worker"),
    ],
)
def test_task_revision_identity_guard_allows_regular_trailing_s_plural(
    plural: str, singular: str
) -> None:
    before = _identity_revision_task(
        title=f"Improve database {plural}",
        resource="repo.database",
        acceptance_ids=("database-contract",),
    )
    after = _identity_revision_task(
        title=f"Improve database {singular}",
        resource="repo.database",
        acceptance_ids=("database-contract",),
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)



def test_task_revision_identity_guard_rejects_title_cased_identifier_as_plural() -> None:
    before = _identity_revision_task(
        title="Maintain Ruby Rails",
        resource="repo.shared",
        goal="Serve HTTP application framework",
    )
    after = _identity_revision_task(
        title="Maintain Ruby Rail",
        resource="repo.shared",
        goal="Track locomotive infrastructure",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


@pytest.mark.parametrize(
    ("plural", "singular"),
    [("schemas", "schema"), ("logos", "logo")],
)
def test_task_revision_identity_guard_allows_lowercase_as_os_regular_plural(
    plural: str, singular: str
) -> None:
    before = _identity_revision_task(
        title=f"Improve database {plural}",
        resource="repo.database",
        acceptance_ids=("database-contract",),
    )
    after = _identity_revision_task(
        title=f"Improve database {singular}",
        resource="repo.database",
        acceptance_ids=("database-contract",),
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)



def test_task_revision_identity_guard_rejects_subject_prefix_before_relocated_identifier() -> None:
    before = _identity_revision_task(
        title="API backup updater service",
        resource="repo.infra",
        goal="Restore backup API health",
    )
    after = _identity_revision_task(
        title="Dashboard API updater service",
        resource="repo.infra",
        goal="Render customer dashboard data",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"



def test_task_revision_identity_guard_rejects_retained_broad_identifier_suffix_swap() -> None:
    before = _identity_revision_task(
        title="API backup updater service",
        resource="repo.infra",
        goal="Restore backup API health",
    )
    after = _identity_revision_task(
        title="API database updater service",
        resource="repo.infra",
        goal="Repair database API state",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"



def test_task_revision_identity_guard_rejects_predicate_inversion_as_weak_subject_support() -> None:
    before = _identity_revision_task(
        title="contracts",
        resource="repo.shared",
        goal="Restore archived snapshots",
    )
    after = _identity_revision_task(
        title="contracts",
        resource="repo.shared",
        goal="Render billing balances",
    )
    shared_contract = {
        "evidence_type": "object",
        "verifier": "manual_observation",
        "verifier_config": {"observation_scope": "shared-contract"},
    }
    before["acceptance"] = [
        {
            "id": "proof-a",
            "assertion": "Verify backup policy prevents customer data deletion in production",
            **shared_contract,
        }
    ]
    after["acceptance"] = [
        {
            **before["acceptance"][0],
            "assertion": "Verify backup policy permits customer data deletion in production",
        }
    ]
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"
