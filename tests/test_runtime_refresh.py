from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from runtime_approval import write_runtime_approval_intent

from bureau import registry_snapshot, runtime_identity
from bureau import runtime_refresh as refresh
from bureau.v2 import StateStore

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
DEPLOYED = "1" * 40
MAIN = "2" * 40
HEAD = "3" * 40


def runtime_authority_spec(task_id: str, *, state: str = "ready") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": task_id,
        "initiative": "TEST-RUNTIME-AUTHORITY",
        "title": "Hermetic runtime refresh authority",
        "goal": "Authorize exactly one target-bound runtime refresh.",
        "state": state,
        "priority": {"lane": "now", "rank": 1},
        "depends_on": [],
        "required_capabilities": ["bureau"],
        "claims": [
            {
                "resource": "component.bureau.runtime",
                "mode": "write",
                "isolation": "worktree",
            }
        ],
        "execution": {
            "mode": "interactive-agent",
            "policy": "review-before-effect",
            "approval": {
                "action_class": "runtime_mutation",
                "required_level": "break_glass",
                "note": "Hermetic single-use runtime authority.",
            },
        },
        "acceptance": [
            {
                "id": "runtime-authority-proof",
                "assertion": "The hermetic runtime authority remains target-bound.",
                "evidence_type": "object",
                "verifier": "manual_observation",
                "verifier_config": {"observation_scope": f"test:{task_id}:runtime-authority"},
            }
        ],
        "rollback": {"strategy": "Preserve the last immutable runtime."},
        "metadata": {
            "runtime_refresh_authority": {
                "schema_version": 1,
                "mode": "single-use-target-bound",
                "single_use": True,
                "successor_task_required_after_terminal": True,
                "required_action_class": "runtime_mutation",
                "required_approval_level": "break_glass",
                "required_claim_resource": "component.bureau.runtime",
                "required_task_state": ["ready", "active"],
                "target_binding": "candidate.target_sha256",
                "forbid_foreign_task_substitution": True,
                "forbid_historical_target_reuse": True,
            }
        },
    }


def seed_authority_store(root: Path, task_id: str, *, state: str = "ready") -> StateStore:
    state_root = root.resolve()
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    spec = runtime_authority_spec(task_id, state=state)
    store.put_task_spec(
        spec,
        idempotency_key=f"seed:{task_id}:{state}",
        expected_revision=None,
        source="test",
    )
    return store


def authority_store_for_intent(intent: dict[str, Any]) -> StateStore:
    binding = intent["authority_state_store"]
    return StateStore(Path(binding["state_db"]), Path(binding["state_root"]))


def revise_authority(
    store: StateStore,
    task_id: str,
    mutate: Any,
    *,
    key: str,
) -> dict[str, Any]:
    current = store.task_spec(task_id)
    assert current is not None
    spec = json.loads(json.dumps(current["spec"]))
    mutate(spec)
    return store.put_task_spec(
        spec,
        idempotency_key=key,
        expected_revision=current["revision"],
        source="test",
    )


def write_manifest(path: Path, source_commit: str = DEPLOYED, **extra: Any) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "kind": "bureau_runtime_deployment",
        "source_commit": source_commit,
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(refresh.canonical_bytes(value))
    return value


def green_pr_detail(main_commit: str = MAIN) -> dict[str, Any]:
    return {
        "number": 42,
        "state": "MERGED",
        "isDraft": False,
        "mergedAt": "2026-07-14T07:30:00Z",
        "mergeCommit": {"oid": main_commit},
        "headRefOid": HEAD,
        "baseRefName": "main",
        "url": "https://example.invalid/pr/42",
        "statusCheckRollup": [
            {"name": "validate (3.10)", "conclusion": "SUCCESS"},
            {"name": "validate (3.12)", "conclusion": "SUCCESS"},
        ],
    }


def github_fixture(
    *,
    main_commit: str = MAIN,
    second_main: str | None = None,
    detail: dict[str, Any] | None = None,
    associated: list[dict[str, Any]] | None = None,
    ahead_by: int = 1,
):
    calls: list[list[str]] = []
    main_reads = 0

    def github(arguments: list[str]) -> Any:
        nonlocal main_reads
        calls.append(arguments)
        joined = " ".join(arguments)
        if joined == "api repos/heimgewebe/bureau/commits/main":
            main_reads += 1
            return {"sha": second_main if main_reads > 1 and second_main else main_commit}
        if joined.endswith(f"repos/heimgewebe/bureau/commits/{main_commit}/pulls"):
            return (
                associated
                if associated is not None
                else [
                    {
                        "number": 42,
                        "merge_commit_sha": main_commit,
                        "merged_at": "2026-07-14T07:30:00Z",
                        "base": {"ref": "main"},
                    }
                ]
            )
        if arguments[:3] == ["pr", "view", "42"]:
            return detail if detail is not None else green_pr_detail(main_commit)
        if joined == f"api repos/heimgewebe/bureau/compare/{DEPLOYED}...{main_commit}":
            return {"ahead_by": ahead_by}
        raise AssertionError(arguments)

    return github, calls


def candidate(tmp_path: Path, **github_options: Any) -> tuple[dict[str, Any], Path]:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_manifest(manifest_path)
    github, _ = github_fixture(**github_options)
    value = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )
    return value, manifest_path


def prepare_candidate_intent(
    tmp_path: Path,
    *,
    task_id: str = "BUR-2026-003-T009",
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    observed, manifest_path = candidate(tmp_path)
    state_root = (tmp_path / "state").resolve()
    authority_store = seed_authority_store(tmp_path / "bureau-state", task_id)
    intent, intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=state_root,
        prefix=(tmp_path / "prefix").resolve(),
        bin_dir=(tmp_path / "bin").resolve(),
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="User explicitly authorized T016 implementation.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id=task_id,
        now=NOW,
        authority_store=authority_store,
    )
    return observed, manifest_path, intent, intent_path


def lease_for(
    root: Path,
    intent: dict[str, Any],
    *,
    schema_version: str = "1",
    lease_contract_version: str | None = "1",
    owner_id: str = "chatgpt-t016",
    expires_at: datetime | None = None,
    omit: set[str] | None = None,
    metadata: dict[str, Any] | None = None,
    metadata_digest: str | None = None,
    current: datetime = NOW,
) -> tuple[dict[str, Any], Path]:
    database = root / "resources.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
        (schema_version,),
    )
    if lease_contract_version is not None:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('resource_lease_contract_version', ?)",
            (lease_contract_version,),
        )
    connection.execute(
        """
        CREATE TABLE leases (
            resource_key TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            acquired_at_unix INTEGER NOT NULL,
            updated_at_unix INTEGER NOT NULL,
            expires_at_unix INTEGER NOT NULL,
            metadata_sha256 TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            reclaimed_from_owner TEXT
        )
        """
    )
    acquired = int((current - timedelta(minutes=1)).timestamp())
    expiry = int((expires_at or current + timedelta(hours=1)).timestamp())
    omitted = omit or set()
    lease_metadata = metadata or {}
    metadata_json = json.dumps(
        lease_metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = metadata_digest or hashlib.sha256(metadata_json.encode("utf-8")).hexdigest()
    for key in intent["required_resource_keys"]:
        if key in omitted:
            continue
        connection.execute(
            "INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (key, owner_id, "test", acquired, acquired, expiry, digest, metadata_json),
        )
    connection.commit()
    connection.close()
    database.chmod(0o600)
    return {"owner_id": owner_id, "task_id": "grabowski-task-t016"}, database


def release_test_leases(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM leases")
    connection.commit()
    connection.close()


def prepare_legacy_cutover(
    tmp_path: Path,
    *,
    current: datetime = NOW,
) -> tuple[dict[str, Any], Path, Path, Path, dict[str, Any]]:
    _, manifest_path, typed_intent, _ = prepare_candidate_intent(tmp_path)
    legacy = dict(typed_intent)
    legacy.pop("runtime_approval")
    legacy.pop("approval_task_id")
    legacy["created_at"] = refresh.isoformat(current)
    legacy["expires_at"] = refresh.isoformat(current + timedelta(minutes=15))
    legacy["nonce"] = "legacy-cutover-test"
    legacy = refresh.bind_digest(legacy, "intent_sha256")
    intent_path = Path(legacy["state_root"]) / "intents" / f"{legacy['intent_sha256']}.json"
    refresh.create_only(intent_path, refresh.canonical_bytes(legacy))
    binding, resource_db = lease_for(tmp_path / "legacy-leases", legacy, current=current)
    normalized = refresh.validate_live_lease_binding(
        legacy, binding, resource_db=resource_db, now=current
    )
    started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": legacy["intent_sha256"],
            "target_sha256": legacy["target_sha256"],
            "main_commit": legacy["main_commit"],
            "lease_binding": normalized,
            "started_at": refresh.isoformat(current),
            "effect_started": False,
        },
        "start_sha256",
    )
    refresh.create_only(
        Path(legacy["state_root"]) / "attempts" / legacy["target_sha256"] / "started.json",
        refresh.canonical_bytes(started),
    )
    return legacy, intent_path, manifest_path, resource_db, started


def apply_successfully(
    tmp_path: Path,
    *,
    task_id: str = "BUR-2026-003-T009",
) -> tuple[
    dict[str, Any],
    Path,
    dict[str, Any],
    Path,
    StateStore,
    dict[str, Any],
    Path,
]:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(
        tmp_path, task_id=task_id
    )
    binding, resource_db = lease_for(tmp_path / "leases", intent)

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        kwargs["workspace"].mkdir(parents=True)
        return {
            "head": MAIN,
            "root": str(kwargs["workspace"]),
            "dirty": False,
            "detached": True,
        }

    def installer(**_: Any) -> dict[str, Any]:
        return {"manifest_sha256": "a" * 64, "rollback": {"directory": "/rollback"}}

    def readback(**_: Any) -> dict[str, Any]:
        return {
            "source_commit": MAIN,
            "manifest_sha256": "a" * 64,
            "check_valid": True,
            "runtime_identity_valid": True,
        }

    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: observed,
        source_preparer=source_preparer,
        installer=installer,
        readback=readback,
    )
    return (
        observed,
        manifest_path,
        intent,
        intent_path,
        authority_store_for_intent(intent),
        result,
        resource_db,
    )


def historical_no_run_success(
    tmp_path: Path,
    *,
    task_id: str,
) -> tuple[dict[str, Any], StateStore, dict[str, Any], Path]:
    _, _, modern_intent, _ = prepare_candidate_intent(tmp_path, task_id=task_id)
    store = authority_store_for_intent(modern_intent)
    intent = dict(modern_intent)
    intent.pop("authority_state_store")
    intent.pop("authority_task_spec")
    intent["nonce"] = "historical-no-run-bootstrap"
    intent = refresh.bind_digest(intent, "intent_sha256")
    intent_path = Path(intent["state_root"]) / "intents" / f"{intent['intent_sha256']}.json"
    refresh.create_only(intent_path, refresh.canonical_bytes(intent))
    lease_binding, resource_db = lease_for(tmp_path / "historical-leases", intent)
    normalized_binding = refresh.validate_live_lease_binding(
        intent, lease_binding, resource_db=resource_db, now=NOW
    )
    started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "lease_binding": normalized_binding,
            "started_at": refresh.isoformat(NOW),
            "effect_started": False,
        },
        "start_sha256",
    )
    attempt_dir = Path(intent["state_root"]) / "attempts" / intent["target_sha256"]
    refresh.create_only(attempt_dir / "started.json", refresh.canonical_bytes(started))
    readback = {
        "source_commit": intent["main_commit"],
        "manifest_sha256": "a" * 64,
        "check_valid": True,
        "runtime_identity_valid": True,
    }
    result = refresh._write_attempt_result(
        attempt_dir / "result.json",
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "source_identity": {"head": intent["main_commit"]},
            "install_receipt": {
                "manifest_sha256": "a" * 64,
                "rollback": {"directory": "/rollback"},
            },
            "readback": readback,
            "lease_binding": normalized_binding,
            "finished_at": refresh.isoformat(NOW),
            "effect_started": True,
            "does_not_establish": [
                "future_runtime_health",
                "future_main_stability",
            ],
        },
    )
    return intent, store, result, resource_db


def historical_runtime_artifacts(
    tmp_path: Path, intent: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path]]:
    prefix = Path(intent["prefix"])
    bin_dir = Path(intent["bin_dir"])
    manifest_path = prefix / "deployment-manifest.json"
    release_id = f"{intent['main_commit'][:12]}-srcfixture"
    release = prefix / "releases" / release_id
    module = release / "src/bureau/runtime_identity.py"
    module.parent.mkdir(parents=True)
    module.write_text("RUNTIME_FIXTURE = True\n", encoding="utf-8")
    cycle = release / "src/bureau_cycle"
    cycle.mkdir(parents=True)
    (cycle / "__init__.py").write_text("CYCLE_FIXTURE = True\n", encoding="utf-8")
    (release / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    schemas = release / "schemas"
    schemas.mkdir()
    (schemas / "task.json").write_text("{}\n", encoding="utf-8")
    systemd = release / "ops/systemd"
    libexec = systemd / "libexec"
    libexec.mkdir(parents=True)
    for name in runtime_identity.SCHEDULER_NAMES:
        (systemd / f"{name}.service").write_text("[Service]\n", encoding="utf-8")
        (systemd / f"{name}.timer").write_text("[Timer]\n", encoding="utf-8")
        executable = libexec / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    package_tree_sha256 = runtime_identity._package_tree_sha256(release)
    assert package_tree_sha256 is not None

    registry_root = prefix / "registry-snapshots" / f"{intent['main_commit'][:12]}-fixture"
    registry_task = registry_root / "registry/tasks/FIXTURE.json"
    registry_task.parent.mkdir(parents=True)
    registry_task.write_text('{"id":"FIXTURE"}\n', encoding="utf-8")
    registry_paths = [Path("registry/tasks/FIXTURE.json")]
    registry_tree_sha256 = registry_snapshot.snapshot_tree_sha256(registry_root, registry_paths)
    assert registry_tree_sha256 is not None
    inventory = registry_root / ".bureau-runtime-snapshot.json"
    inventory_value = {
        "schema_version": 1,
        "kind": "bureau_registry_snapshot",
        "source_commit": intent["main_commit"],
        "tree_sha256": registry_tree_sha256,
        "paths": [path.as_posix() for path in registry_paths],
    }
    inventory.write_bytes(refresh.canonical_bytes(inventory_value))
    inventory_sha256 = refresh.sha256_bytes(inventory.read_bytes())

    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher_contents = {
        "bureau": b"#!/bin/sh\necho historical-bureau\n",
        "bureau-runtime-refresh": b"#!/bin/sh\necho historical-refresh\n",
        "bureau-status-capsule": b"#!/bin/sh\necho historical-status\n",
    }
    for name, content in launcher_contents.items():
        path = bin_dir / name
        path.write_bytes(content)
        path.chmod(0o755)

    rollback = {
        "directory": str(prefix / "backups/pre-historical"),
        "manifest": None,
        "launcher": None,
        "runtime_refresh_launcher": None,
        "status_capsule_launcher": None,
    }
    runtime_approval = {
        "schema_version": 1,
        "allowed": True,
        "reference": intent["target_sha256"],
    }
    manifest = {
        "schema_version": 1,
        "kind": "bureau_runtime_deployment",
        "release_id": release_id,
        "source_repository": str(tmp_path / "source"),
        "source_commit": intent["main_commit"],
        "package_tree_sha256": package_tree_sha256,
        "immutable_release_path": str(release),
        "module_path": str(module),
        "module_sha256": refresh.sha256_bytes(module.read_bytes()),
        "canonical_registry_root": str(registry_root),
        "canonical_registry_inventory_path": str(inventory),
        "canonical_registry_inventory_sha256": inventory_sha256,
        "canonical_registry_tree_sha256": registry_tree_sha256,
        "launcher_path": str(bin_dir / "bureau"),
        "runtime_refresh_launcher_path": str(bin_dir / "bureau-runtime-refresh"),
        "status_capsule_launcher_path": str(bin_dir / "bureau-status-capsule"),
        "installed_at": refresh.isoformat(NOW),
        "runtime_approval": runtime_approval,
        "previous_manifest_sha256": None,
        "rollback": rollback,
    }
    manifest[refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD] = refresh.payload_digest(
        manifest, refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(refresh.canonical_bytes(manifest))
    manifest_sha256 = refresh.sha256_bytes(manifest_path.read_bytes())
    receipt = {
        "schema_version": 1,
        "kind": "bureau_runtime_install_receipt",
        "release_id": release_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "launcher_path": str(bin_dir / "bureau"),
        "launcher_sha256": refresh.sha256_bytes((bin_dir / "bureau").read_bytes()),
        "launcher_written": True,
        "runtime_refresh_launcher_path": str(bin_dir / "bureau-runtime-refresh"),
        "runtime_refresh_launcher_sha256": refresh.sha256_bytes(
            (bin_dir / "bureau-runtime-refresh").read_bytes()
        ),
        "runtime_refresh_launcher_written": True,
        "status_capsule_launcher_path": str(bin_dir / "bureau-status-capsule"),
        "status_capsule_launcher_sha256": refresh.sha256_bytes(
            (bin_dir / "bureau-status-capsule").read_bytes()
        ),
        "status_capsule_launcher_written": True,
        "package_tree_sha256": package_tree_sha256,
        "canonical_registry_root": str(registry_root),
        "canonical_registry_tree_sha256": registry_tree_sha256,
        "rollback": rollback,
        "runtime_approval": runtime_approval,
        "installed_at": refresh.isoformat(NOW),
    }
    receipt_path = prefix / "receipts" / f"{release_id}-{manifest_sha256[:12]}.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(refresh.canonical_bytes(receipt))
    install_receipt = {**receipt, "receipt_path": str(receipt_path)}
    historical_readback = refresh.readback_historical_install(
        expected_commit=intent["main_commit"],
        prefix=prefix,
        bin_dir=bin_dir,
        install_receipt=install_receipt,
    )

    backup = prefix / "backups" / "successor-deploy"
    backup.mkdir(parents=True)
    shutil.copy2(manifest_path, backup / "deployment-manifest.json")
    for name in launcher_contents:
        shutil.copy2(bin_dir / name, backup / name)
    successor_commit = "9" * 40
    successor_release_id = f"{successor_commit[:12]}-srcfixture"
    successor_release = prefix / "releases" / successor_release_id
    shutil.copytree(release, successor_release)
    successor_module = successor_release / "src/bureau/runtime_identity.py"
    successor_registry = prefix / "registry-snapshots" / f"{successor_commit[:12]}-fixture"
    shutil.copytree(registry_root, successor_registry)
    successor_inventory = successor_registry / ".bureau-runtime-snapshot.json"
    successor_inventory_value = {**inventory_value, "source_commit": successor_commit}
    successor_inventory.write_bytes(refresh.canonical_bytes(successor_inventory_value))
    successor_manifest = {
        **manifest,
        "release_id": successor_release_id,
        "source_commit": successor_commit,
        "immutable_release_path": str(successor_release),
        "module_path": str(successor_module),
        "module_sha256": refresh.sha256_bytes(successor_module.read_bytes()),
        "canonical_registry_root": str(successor_registry),
        "canonical_registry_inventory_path": str(successor_inventory),
        "canonical_registry_inventory_sha256": refresh.sha256_bytes(
            successor_inventory.read_bytes()
        ),
        "previous_manifest_sha256": manifest_sha256,
    }
    successor_manifest[refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD] = refresh.payload_digest(
        successor_manifest, refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD
    )
    manifest_path.write_bytes(refresh.canonical_bytes(successor_manifest))
    successor_bytes = manifest_path.read_bytes()
    assert runtime_identity._package_tree_sha256(successor_release) == package_tree_sha256
    assert registry_snapshot.canonical_registry_identity(successor_manifest)["valid"] is True
    for name in launcher_contents:
        path = bin_dir / name
        path.write_bytes(f"#!/bin/sh\necho successor-{name}\n".encode())
        path.chmod(0o755)
    assert manifest_path.read_bytes() == successor_bytes
    return install_receipt, historical_readback, {
        "manifest": manifest_path,
        "backup": backup,
        "release": release,
        "registry_task": registry_task,
        "receipt": receipt_path,
    }


def replace_historical_result(
    intent: dict[str, Any],
    result: dict[str, Any],
    *,
    install_receipt: dict[str, Any],
    readback: dict[str, Any],
) -> dict[str, Any]:
    result_path = Path(intent["state_root"]) / "attempts" / intent["target_sha256"] / "result.json"
    result_path.unlink()
    payload = dict(result)
    payload.pop("result_sha256", None)
    payload["install_receipt"] = install_receipt
    payload["readback"] = readback
    return refresh._write_attempt_result(result_path, payload)


def test_observe_reports_already_current_without_pr_lookup(tmp_path: Path) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    write_manifest(manifest, MAIN)
    calls: list[list[str]] = []

    def github(arguments: list[str]) -> Any:
        calls.append(arguments)
        return {"sha": MAIN}

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=github,
    )

    assert result["status"] == "already_current"
    assert result["lag_commits"] == 0
    assert result["recovery_action"] == {
        "action": "none",
        "eligible": False,
        "requires_authorization": False,
    }
    assert calls == [["api", "repos/heimgewebe/bureau/commits/main"]]
    refresh.verify_digest(result, "observation_sha256")


def test_observe_binds_exact_merged_main_and_green_ci(tmp_path: Path) -> None:
    result, _ = candidate(tmp_path)

    assert result["status"] == "candidate"
    assert result["main_commit"] == MAIN
    assert result["pull_request"] == {
        "number": 42,
        "url": "https://example.invalid/pr/42",
        "head_commit": HEAD,
        "merge_commit": MAIN,
    }
    assert set(result["check_summary"]) == {
        "validate (3.10)",
        "validate (3.12)",
    }
    assert all(item["state"] == "success" for item in result["check_summary"].values())
    assert result["lag_commits"] == 1
    assert result["recovery_action"] == {
        "action": "prepare-intent",
        "eligible": True,
        "requires_authorization": True,
    }
    assert len(result["target_sha256"]) == 64


def test_observe_alerts_after_freshness_slo(tmp_path: Path) -> None:
    result, manifest = candidate(tmp_path)
    github, _ = github_fixture()
    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW + timedelta(hours=3),
        slo_seconds=3600,
        github=github,
    )
    assert result["status"] == "alert"
    assert result["age_seconds"] > result["slo_seconds"]
    assert result["recovery_action"]["action"] == "prepare-intent"
    assert result["recovery_action"]["eligible"] is True


def test_observe_blocks_failed_or_missing_ci(tmp_path: Path) -> None:
    detail = green_pr_detail()
    detail["statusCheckRollup"] = [{"name": "validate (3.10)", "conclusion": "FAILURE"}]
    result, _ = candidate(tmp_path, detail=detail)

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["required-ci-not-green"]
    assert result["recovery_action"] == {
        "action": "resolve-reason-codes",
        "eligible": False,
        "reason_codes": ["required-ci-not-green"],
        "requires_authorization": False,
    }
    assert result["check_summary"]["validate (3.10)"]["state"] == "failure"
    assert result["check_summary"]["validate (3.12)"]["state"] == "missing"


def test_observe_rejects_skipped_required_ci(tmp_path: Path) -> None:
    detail = green_pr_detail()
    detail["statusCheckRollup"] = [
        {"name": "validate (3.10)", "conclusion": "SUCCESS"},
        {"name": "validate (3.12)", "conclusion": "SKIPPED"},
    ]
    result, _ = candidate(tmp_path, detail=detail)

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["required-ci-not-green"]
    assert result["check_summary"]["validate (3.12)"]["state"] == "failure"


def test_observe_blocks_ambiguous_pr_and_main_drift(tmp_path: Path) -> None:
    result, _ = candidate(
        tmp_path,
        associated=[],
        second_main="4" * 40,
    )

    assert result["status"] == "blocked"
    assert "merged-main-pr-ambiguous" in result["reason_codes"]
    assert "main-changed-during-observation" in result["reason_codes"]


def test_prepare_intent_is_hash_bound_and_requires_authorization(tmp_path: Path) -> None:
    observed, _, intent, intent_path = prepare_candidate_intent(tmp_path)

    assert intent_path.is_file()
    assert intent["target_sha256"] == observed["target_sha256"]
    assert intent["expected_deployed_source_commit"] == DEPLOYED
    assert intent["required_resource_keys"] == sorted(intent["required_resource_keys"])
    assert f"path:{tmp_path.resolve() / 'bin/bureau'}" in intent["required_resource_keys"]
    assert (
        f"path:{tmp_path.resolve() / 'bin/bureau-status-capsule'}"
        in intent["required_resource_keys"]
    )
    assert intent["runtime_approval"]["allowed"] is True
    assert intent["runtime_approval"]["required_level"] == "break_glass"
    assert intent["runtime_approval"]["expected_reference"] == observed["target_sha256"]
    refresh.verify_digest(intent, "intent_sha256")

    with pytest.raises(refresh.RuntimeRefreshError) as denied:
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "denied-state").resolve(),
            prefix=(tmp_path / "denied-prefix").resolve(),
            bin_dir=(tmp_path / "denied-bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="chatgpt",
            authorization="ordinary operator authorization",
            break_glass=False,
            approval_reference=observed["target_sha256"],
            approval_task_id="BUR-2026-003-T009",
            now=NOW,
        )
    assert denied.value.code == "runtime-approval-required"

    with pytest.raises(refresh.RuntimeRefreshError, match="authorization"):
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "other-state").resolve(),
            prefix=(tmp_path / "other-prefix").resolve(),
            bin_dir=(tmp_path / "other-bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="",
            authorization="",
            now=NOW,
        )


def test_prepare_uses_authoritative_state_store_over_stale_registry_snapshot(
    tmp_path: Path,
) -> None:
    task_id = "BUR-STALE-SNAPSHOT-AUTHORITY"
    observed, _ = candidate(tmp_path)
    snapshot = tmp_path / "installed-registry/registry/tasks" / f"{task_id}.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(refresh.canonical_bytes(runtime_authority_spec(task_id)))
    store = seed_authority_store(tmp_path / "bureau-state", task_id)
    revise_authority(
        store,
        task_id,
        lambda spec: spec.__setitem__("state", "superseded"),
        key="supersede-before-prepare",
    )
    authoritative_before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="operator",
            authorization="Exact stale-snapshot regression approval.",
            break_glass=True,
            approval_reference=observed["target_sha256"],
            approval_task_id=task_id,
            now=NOW,
            authority_store=store,
        )

    assert json.loads(snapshot.read_text(encoding="utf-8"))["state"] == "ready"
    assert error.value.code == "authority-task-terminal"
    assert store.task_spec(task_id) == authoritative_before
    assert not (tmp_path / "state/intents").exists()


def test_apply_rechecks_exact_authoritative_revision_before_any_effect(
    tmp_path: Path,
) -> None:
    _observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    store = authority_store_for_intent(intent)
    revise_authority(
        store,
        intent["approval_task_id"],
        lambda spec: spec["metadata"].__setitem__("drift_marker", "new revision"),
        key="authority-drift-before-apply",
    )
    binding, resource_db = lease_for(tmp_path / "leases", intent)

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run after authority drift"),
            source_preparer=lambda **_: pytest.fail("source effect must not run"),
            installer=lambda **_: pytest.fail("installer effect must not run"),
        )

    assert error.value.code == "authority-task-drift"
    assert not (Path(intent["state_root"]) / "attempts" / intent["target_sha256"]).exists()


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("superseded", "authority-task-terminal"),
        ("verified", "authority-task-terminal"),
        ("wrong-task", "authority-task-missing"),
        ("wrong-target", "authority-wrong-target"),
    ],
)
def test_prepare_rejects_non_authorizing_tasks_before_intent_effect(
    tmp_path: Path, case: str, expected_code: str
) -> None:
    task_id = "BUR-EXACT-RUNTIME-AUTHORITY"
    observed, _ = candidate(tmp_path)
    store = seed_authority_store(
        tmp_path / "bureau-state",
        "BUR-OTHER-TASK" if case == "wrong-task" else task_id,
        state=case if case in {"superseded", "verified"} else "ready",
    )
    if case == "wrong-target":
        current = store.task_spec(task_id)
        assert current is not None

        def bind_wrong_target(spec: dict[str, Any]) -> None:
            spec["metadata"]["runtime_refresh_authority"]["target_binding_receipt"] = {
                "schema_version": 1,
                "kind": refresh.RUNTIME_AUTHORITY_BINDING_KIND,
                "task_id": task_id,
                "authority_revision": current["revision"],
                "authority_spec_sha256": current["spec_sha256"],
                "target_sha256": "f" * 64,
                "intent_sha256": "e" * 64,
                "bound_at": refresh.isoformat(NOW),
            }

        revise_authority(store, task_id, bind_wrong_target, key="wrong-target-binding")
    before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="operator",
            authorization="Exact negative authority approval.",
            break_glass=True,
            approval_reference=observed["target_sha256"],
            approval_task_id=task_id,
            now=NOW,
            authority_store=store,
        )

    assert error.value.code == expected_code
    assert store.task_spec(task_id) == before
    assert not (tmp_path / "state/intents").exists()


def test_prepare_intent_omits_stable_launchers_and_requires_only_drift(tmp_path: Path) -> None:
    observed, manifest_path = candidate(tmp_path)
    prefix = (tmp_path / "prefix").resolve()
    bin_dir = (tmp_path / "bin").resolve()
    bin_dir.mkdir(parents=True)
    for name, entrypoint in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS:
        path = bin_dir / name
        path.write_bytes(refresh.stable_launcher_bytes(manifest_path, entrypoint))
        path.chmod(0o755)

    state_root = (tmp_path / "stable-state").resolve()
    stable_authority = seed_authority_store(
        tmp_path / "stable-bureau-state", "BUR-STABLE-LAUNCHER-TEST"
    )
    intent, _ = refresh.prepare_intent(
        candidate=observed,
        state_root=state_root,
        prefix=prefix,
        bin_dir=bin_dir,
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="Explicit stable-launcher migration approval.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-STABLE-LAUNCHER-TEST",
        now=NOW,
        authority_store=stable_authority,
    )
    launcher_keys = {f"path:{bin_dir / name}" for name, _ in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS}
    assert launcher_keys.isdisjoint(intent["required_resource_keys"])

    status_capsule = bin_dir / "bureau-status-capsule"
    status_capsule.write_text("corrupt\n", encoding="utf-8")
    drift_state = (tmp_path / "drift-state").resolve()
    repair_authority = seed_authority_store(
        tmp_path / "repair-bureau-state", "BUR-STABLE-LAUNCHER-REPAIR"
    )
    drift_intent, _ = refresh.prepare_intent(
        candidate=observed,
        state_root=drift_state,
        prefix=prefix,
        bin_dir=bin_dir,
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="Explicit launcher repair approval.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-STABLE-LAUNCHER-REPAIR",
        now=NOW,
        authority_store=repair_authority,
    )
    assert f"path:{status_capsule}" in drift_intent["required_resource_keys"]
    assert f"path:{bin_dir / 'bureau'}" not in drift_intent["required_resource_keys"]
    assert (
        f"path:{bin_dir / 'bureau-runtime-refresh'}" not in drift_intent["required_resource_keys"]
    )


def test_apply_blocks_launcher_drift_absent_from_intent_before_observer(tmp_path: Path) -> None:
    observed, manifest_path = candidate(tmp_path)
    prefix = (tmp_path / "prefix").resolve()
    bin_dir = (tmp_path / "bin").resolve()
    bin_dir.mkdir(parents=True)
    for name, entrypoint in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS:
        path = bin_dir / name
        path.write_bytes(refresh.stable_launcher_bytes(manifest_path, entrypoint))
        path.chmod(0o755)
    state_root = (tmp_path / "state").resolve()
    authority_store = seed_authority_store(tmp_path / "bureau-state", "BUR-STABLE-LAUNCHER-DRIFT")
    intent, intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=state_root,
        prefix=prefix,
        bin_dir=bin_dir,
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="Explicit drift-gate approval.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-STABLE-LAUNCHER-DRIFT",
        now=NOW,
        authority_store=authority_store,
    )
    status_capsule = bin_dir / "bureau-status-capsule"
    status_capsule.write_text("drifted\n", encoding="utf-8")
    binding, resource_db = lease_for(tmp_path / "drift-leases", intent)
    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=state_root,
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run after launcher drift"),
        )
    assert error.value.code == "launcher-drift-after-intent"
    assert error.value.details["resource_keys"] == [f"path:{status_capsule}"]


def test_runtime_approval_requires_minimum_remaining_lifetime(tmp_path: Path) -> None:
    observed, _ = candidate(tmp_path)
    authority_store = seed_authority_store(tmp_path / "bureau-state", "BUR-2026-003-T009")
    intent, intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=(tmp_path / "short-state").resolve(),
        prefix=(tmp_path / "short-prefix").resolve(),
        bin_dir=(tmp_path / "short-bin").resolve(),
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="Explicit short-lived break-glass approval.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-2026-003-T009",
        ttl_seconds=599,
        now=NOW,
        authority_store=authority_store,
    )

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.validate_runtime_approval_intent(
            intent_path,
            now=NOW,
            minimum_remaining_seconds=600,
        )

    assert error.value.code == "runtime-approval-validity-too-short"
    assert error.value.details == {
        "remaining_seconds": 599,
        "minimum_remaining_seconds": 600,
    }
    assert not (Path(intent["state_root"]) / "attempts" / intent["target_sha256"]).exists()


def test_prepare_intent_rejects_tampered_or_blocked_candidate(tmp_path: Path) -> None:
    observed, _ = candidate(tmp_path)
    observed["main_commit"] = "9" * 40
    with pytest.raises(refresh.RuntimeRefreshError, match="does not match"):
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="chatgpt",
            authorization="explicit authorization",
            now=NOW,
        )

    blocked, _ = candidate(
        tmp_path / "blocked",
        associated=[],
    )
    with pytest.raises(refresh.RuntimeRefreshError) as blocked_error:
        refresh.prepare_intent(
            candidate=blocked,
            state_root=(tmp_path / "blocked-state").resolve(),
            prefix=(tmp_path / "blocked-prefix").resolve(),
            bin_dir=(tmp_path / "blocked-bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="chatgpt",
            authorization="explicit authorization",
            now=NOW,
        )
    assert blocked_error.value.code == "candidate-not-deployable"


def test_legacy_cutover_requires_started_attempt_manifest_and_live_leases(
    tmp_path: Path,
) -> None:
    legacy, _, manifest_path, resource_db, started = prepare_legacy_cutover(tmp_path)

    decision = refresh.validate_legacy_runtime_refresh_bootstrap(
        state_root=Path(legacy["state_root"]),
        resource_db=resource_db,
        expected_source_commit=legacy["main_commit"],
        prefix=Path(legacy["prefix"]),
        bin_dir=Path(legacy["bin_dir"]),
        manifest_path=manifest_path,
        now=NOW,
    )

    assert decision["allowed"] is True
    assert decision["required_level"] == "legacy_runtime_operator_gate"
    assert decision["expected_reference"] == legacy["target_sha256"]
    assert decision["legacy_cutover"]["intent_sha256"] == legacy["intent_sha256"]
    assert decision["legacy_cutover"]["start_sha256"] == started["start_sha256"]

    result_path = Path(legacy["state_root"]) / "attempts" / legacy["target_sha256"] / "result.json"
    refresh.create_only(
        result_path,
        refresh.canonical_bytes({"status": "failed-before-cutover"}),
    )
    with pytest.raises(refresh.RuntimeRefreshError) as completed:
        refresh.validate_legacy_runtime_refresh_bootstrap(
            state_root=Path(legacy["state_root"]),
            resource_db=resource_db,
            expected_source_commit=legacy["main_commit"],
            prefix=Path(legacy["prefix"]),
            bin_dir=Path(legacy["bin_dir"]),
            manifest_path=manifest_path,
            now=NOW,
        )
    assert completed.value.code == "runtime-approval-missing"


def test_legacy_cutover_rejects_reacquired_leases(tmp_path: Path) -> None:
    legacy, _, manifest_path, resource_db, _ = prepare_legacy_cutover(tmp_path)
    connection = sqlite3.connect(resource_db)
    connection.execute(
        """
        UPDATE leases
        SET acquired_at_unix = acquired_at_unix + 1,
            updated_at_unix = updated_at_unix + 1,
            expires_at_unix = expires_at_unix + 1
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.validate_legacy_runtime_refresh_bootstrap(
            state_root=Path(legacy["state_root"]),
            resource_db=resource_db,
            expected_source_commit=legacy["main_commit"],
            prefix=Path(legacy["prefix"]),
            bin_dir=Path(legacy["bin_dir"]),
            manifest_path=manifest_path,
            now=NOW,
        )

    assert error.value.code == "runtime-approval-missing"


def test_lease_binding_requires_live_complete_private_database(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    binding, incomplete_db = lease_for(
        tmp_path / "incomplete",
        intent,
        omit={intent["required_resource_keys"][-1]},
    )
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.validate_live_lease_binding(intent, binding, resource_db=incomplete_db, now=NOW)
    assert missing.value.code == "lease-resources-missing"

    binding, expired_db = lease_for(
        tmp_path / "expired",
        intent,
        expires_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(refresh.RuntimeRefreshError) as expiry:
        refresh.validate_live_lease_binding(intent, binding, resource_db=expired_db, now=NOW)
    assert expiry.value.code == "lease-expired"

    binding, live_db = lease_for(tmp_path / "live", intent)
    observed = refresh.validate_live_lease_binding(intent, binding, resource_db=live_db, now=NOW)
    assert observed["owner_id"] == binding["owner_id"]
    assert observed["resource_keys"] == intent["required_resource_keys"]
    assert len(observed["lease_snapshots"]) == len(intent["required_resource_keys"])

    live_db.chmod(0o644)
    with pytest.raises(refresh.RuntimeRefreshError) as public:
        refresh.validate_live_lease_binding(intent, binding, resource_db=live_db, now=NOW)
    assert public.value.code == "lease-database-mode-invalid"


def test_apply_requires_status_capsule_launcher_lease_before_effect(tmp_path: Path) -> None:
    _observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    status_key = f"path:{Path(intent['bin_dir']) / 'bureau-status-capsule'}"
    assert status_key in intent["required_resource_keys"]

    binding, missing_db = lease_for(tmp_path / "status-missing", intent, omit={status_key})
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=missing_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run"),
            source_preparer=lambda **_: pytest.fail("source preparation must not run"),
            installer=lambda **_: pytest.fail("installer must not run"),
        )
    assert missing.value.code == "lease-resources-missing"

    binding, foreign_db = lease_for(tmp_path / "status-foreign", intent)
    connection = sqlite3.connect(foreign_db)
    connection.execute(
        "UPDATE leases SET owner_id = ? WHERE resource_key = ?",
        ("foreign-owner", status_key),
    )
    connection.commit()
    connection.close()
    with pytest.raises(refresh.RuntimeRefreshError) as foreign:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=foreign_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run"),
            source_preparer=lambda **_: pytest.fail("source preparation must not run"),
            installer=lambda **_: pytest.fail("installer must not run"),
        )
    assert foreign.value.code == "lease-owner-mismatch"


def test_lease_binding_verifies_metadata_digest_and_required_binding(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    required = {
        "task_id": "grabowski-task-t016",
        "operation": "runtime-refresh",
    }
    binding, live_db = lease_for(tmp_path / "metadata-live", intent, metadata=required)
    observed = refresh.validate_live_lease_binding(
        intent,
        binding,
        resource_db=live_db,
        now=NOW,
        required_metadata=required,
    )
    assert (
        observed["required_metadata_sha256"]
        == hashlib.sha256(
            json.dumps(required, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    assert all("metadata_json" not in item for item in observed["lease_snapshots"])

    binding, tampered_db = lease_for(
        tmp_path / "metadata-tampered",
        intent,
        metadata=required,
        metadata_digest="0" * 64,
    )
    with pytest.raises(refresh.RuntimeRefreshError) as digest_error:
        refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=tampered_db,
            now=NOW,
            required_metadata=required,
        )
    assert digest_error.value.code == "lease-metadata-digest-mismatch"

    binding, wrong_db = lease_for(
        tmp_path / "metadata-wrong-binding",
        intent,
        metadata={**required, "operation": "other"},
    )
    with pytest.raises(refresh.RuntimeRefreshError) as binding_error:
        refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=wrong_db,
            now=NOW,
            required_metadata=required,
        )
    assert binding_error.value.code == "lease-metadata-binding-mismatch"
    assert binding_error.value.details["mismatched"] == {
        "operation": {"expected": "runtime-refresh", "observed": "other"}
    }


def test_lease_binding_uses_lease_contract_not_aggregate_schema(
    tmp_path: Path,
) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)

    for schema_version in ("1", "2", "3", "4", "99"):
        binding, resource_db = lease_for(
            tmp_path / f"schema-{schema_version}",
            intent,
            schema_version=schema_version,
        )
        if schema_version == "4":
            with sqlite3.connect(resource_db) as connection:
                connection.execute(
                    "CREATE TABLE unrelated_additive_state("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO unrelated_additive_state VALUES('proof', 'preserved')"
                )
                connection.commit()
        observed = refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=resource_db,
            now=NOW,
        )
        assert observed["resource_db_schema_version"] == schema_version
        assert observed["resource_lease_contract_version"] == "1"


def test_lease_binding_fails_closed_on_missing_future_or_malformed_contract(
    tmp_path: Path,
) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)

    binding, missing_db = lease_for(
        tmp_path / "contract-missing",
        intent,
        schema_version="4",
        lease_contract_version=None,
    )
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.validate_live_lease_binding(intent, binding, resource_db=missing_db, now=NOW)
    assert missing.value.code == "lease-contract-metadata-missing"
    assert missing.value.details["aggregate_schema"] == "4"
    assert missing.value.details["observed_rows"] == 0

    binding, future_db = lease_for(
        tmp_path / "contract-future",
        intent,
        schema_version="4",
        lease_contract_version="2",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as future:
        refresh.validate_live_lease_binding(intent, binding, resource_db=future_db, now=NOW)
    assert future.value.code == "lease-contract-version-unsupported"
    assert future.value.details["observed"] == "2"
    assert future.value.details["supported"] == ["1"]
    assert future.value.details["aggregate_schema"] == "4"

    binding, malformed_db = lease_for(
        tmp_path / "contract-malformed",
        intent,
        lease_contract_version="not-a-version",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as malformed:
        refresh.validate_live_lease_binding(intent, binding, resource_db=malformed_db, now=NOW)
    assert malformed.value.code == "lease-contract-version-malformed"


def test_lease_contract_is_validated_before_lease_rows_are_read(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    database = tmp_path / "metadata-only/resources.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES('schema_version', '4')")
        connection.commit()
    database.chmod(0o600)
    binding = {"owner_id": "chatgpt-t016", "task_id": "grabowski-task-t016"}

    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.validate_live_lease_binding(intent, binding, resource_db=database, now=NOW)

    assert missing.value.code == "lease-contract-metadata-missing"


def git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def create_remote(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "initial")
    head = git(source, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(source), str(bare))
    return bare, head


def test_lease_database_rejects_file_and_parent_symlinks(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    binding, live_db = lease_for(tmp_path / "real", intent)

    file_link = tmp_path / "resources-link.sqlite3"
    file_link.symlink_to(live_db)
    with pytest.raises(refresh.RuntimeRefreshError) as file_error:
        refresh.validate_live_lease_binding(intent, binding, resource_db=file_link, now=NOW)
    assert file_error.value.code == "lease-database-symlink"

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(live_db.parent, target_is_directory=True)
    with pytest.raises(refresh.RuntimeRefreshError) as parent_error:
        refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=linked_parent / live_db.name,
            now=NOW,
        )
    assert parent_error.value.code == "lease-database-parent-symlink"


def test_environment_cannot_redirect_production_lease_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRABOWSKI_RESOURCE_DB", "/tmp/fake-resources.sqlite3")
    assert (
        Path("~/.local/state/grabowski/resources.sqlite3").expanduser()
        == refresh.DEFAULT_GRABOWSKI_RESOURCE_DB
    )


def test_prepare_source_checkout_is_clean_detached_and_exact(tmp_path: Path) -> None:
    remote, head = create_remote(tmp_path)
    workspace = tmp_path / "state/workspaces" / head

    identity = refresh.prepare_source_checkout(
        remote_url=str(remote),
        workspace=workspace,
        expected_commit=head,
        workspaces_root=tmp_path / "state/workspaces",
    )

    assert identity["head"] == head
    assert identity["origin_main"] == head
    assert identity["detached"] is True
    assert identity["dirty"] is False
    assert git(workspace, "status", "--porcelain=v1") == ""

    (workspace / "foreign.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(refresh.RuntimeRefreshError) as dirty:
        refresh.validate_source_checkout(workspace, head, str(remote))
    assert dirty.value.code == "source-dirty"


def test_prepare_source_checkout_fails_closed_on_origin_drift(tmp_path: Path) -> None:
    remote, _head = create_remote(tmp_path)
    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.prepare_source_checkout(
            remote_url=str(remote),
            workspace=tmp_path / "state/workspaces/wrong",
            expected_commit="f" * 40,
            workspaces_root=tmp_path / "state/workspaces",
        )
    assert error.value.code == "origin-main-drift"


def test_apply_success_is_one_shot_and_preserves_foreign_dirty_checkout(
    tmp_path: Path,
) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    dirty_checkout = tmp_path / "conventional-main"
    dirty_checkout.mkdir()
    sentinel = dirty_checkout / "foreign-change.txt"
    sentinel.write_text("do not touch", encoding="utf-8")
    calls = {"source": 0, "install": 0, "readback": 0}

    def observer(**_: Any) -> dict[str, Any]:
        return observed

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        calls["source"] += 1
        workspace = kwargs["workspace"]
        workspace.mkdir(parents=True)
        return {"root": str(workspace), "head": MAIN, "dirty": False, "detached": True}

    def installer(**_: Any) -> dict[str, Any]:
        calls["install"] += 1
        return {"manifest_sha256": "a" * 64, "rollback": {"directory": "/rollback"}}

    def readback(**_: Any) -> dict[str, Any]:
        calls["readback"] += 1
        return {"source_commit": MAIN, "check_valid": True, "runtime_identity_valid": True}

    lease_binding, resource_db = lease_for(tmp_path / "leases", intent)
    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
        source_preparer=source_preparer,
        installer=installer,
        readback=readback,
    )
    reused = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
        source_preparer=source_preparer,
        installer=installer,
        readback=readback,
    )

    assert result["status"] == "deployed"
    assert reused["status"] == "deployed"
    assert reused["reused"] is True
    assert calls == {"source": 1, "install": 1, "readback": 1}
    assert not Path(intent["workspace"]).exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    refresh.verify_digest(result, "result_sha256")


def test_success_consumes_authority_and_exact_result_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    (
        observed,
        manifest_path,
        intent,
        intent_path,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path)
    current = store.task_spec(intent["approval_task_id"])
    assert current is not None
    authority = current["spec"]["metadata"]["runtime_refresh_authority"]
    consumption = authority["consumption"]
    assert consumption == {
        "schema_version": 1,
        "kind": refresh.RUNTIME_AUTHORITY_CONSUMPTION_KIND,
        "status": "consumed",
        "task_id": intent["approval_task_id"],
        "authority_revision": intent["authority_task_spec"]["revision"],
        "authority_spec_sha256": intent["authority_task_spec"]["spec_sha256"],
        "target_sha256": intent["target_sha256"],
        "intent_sha256": intent["intent_sha256"],
        "result_sha256": result["result_sha256"],
        "consumed_at": refresh.isoformat(NOW),
    }
    revision_after_success = current["revision"]

    replay = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding={"owner_id": "ignored", "task_id": "ignored"},
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW + timedelta(seconds=1),
        observer=lambda **_: pytest.fail("result replay must not observe GitHub"),
        source_preparer=lambda **_: pytest.fail("result replay must not prepare source"),
        installer=lambda **_: pytest.fail("result replay must not install"),
    )
    assert replay["result_sha256"] == result["result_sha256"]
    assert replay["reused"] is True
    assert store.task_spec(intent["approval_task_id"])["revision"] == revision_after_success

    later = dict(observed)
    later["main_commit"] = "4" * 40
    later["target_sha256"] = "5" * 64
    later = refresh.bind_digest(later, "observation_sha256")
    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.prepare_intent(
            candidate=later,
            state_root=(tmp_path / "later-state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="operator",
            authorization="Attempted later target approval.",
            break_glass=True,
            approval_reference=later["target_sha256"],
            approval_task_id=intent["approval_task_id"],
            now=NOW,
            authority_store=store,
        )
    assert error.value.code == "authority-already-consumed"


def test_consumption_rejects_task_spec_drift_after_target_binding(
    tmp_path: Path,
) -> None:
    observed, _manifest_path, intent, _intent_path = prepare_candidate_intent(tmp_path)
    store = authority_store_for_intent(intent)
    bound = refresh.bind_runtime_refresh_authority(store=store, intent=intent, now=NOW)
    result = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "authority_task_spec": bound,
            "finished_at": refresh.isoformat(NOW),
            "effect_started": True,
            "lease_binding": {"lease_binding_sha256": "a" * 64},
        },
        "result_sha256",
    )
    revise_authority(
        store,
        intent["approval_task_id"],
        lambda spec: spec.__setitem__("title", "Concurrent post-binding drift"),
        key="post-binding-drift",
    )

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.consume_runtime_refresh_authority(
            store=store, intent=intent, result=result, now=NOW
        )

    assert caught.value.code == "authority-task-drift-after-effect"
    current = store.task_spec(intent["approval_task_id"])
    assert current is not None
    assert current["spec"]["metadata"]["runtime_refresh_authority"].get("consumption") is None
    assert observed["target_sha256"] == intent["target_sha256"]


def test_tampered_result_and_consumption_are_rejected_on_replay(tmp_path: Path) -> None:
    (
        _,
        manifest_path,
        intent,
        intent_path,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path)
    result_path = Path(intent["state_root"]) / "attempts" / intent["target_sha256"] / "result.json"
    tampered_result = dict(result)
    tampered_result["status"] = "failed"
    result_path.write_bytes(refresh.canonical_bytes(tampered_result))
    with pytest.raises(refresh.RuntimeRefreshError) as result_error:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding={"owner_id": "ignored", "task_id": "ignored"},
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
        )
    assert result_error.value.code == "digest-mismatch"

    result_path.write_bytes(refresh.canonical_bytes(result))

    def tamper_consumption(spec: dict[str, Any]) -> None:
        spec["metadata"]["runtime_refresh_authority"]["consumption"]["result_sha256"] = "f" * 64

    revise_authority(
        store,
        intent["approval_task_id"],
        tamper_consumption,
        key="tampered-consumption",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as consumption_error:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding={"owner_id": "ignored", "task_id": "ignored"},
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
        )
    assert consumption_error.value.code == "authority-consumption-mismatch"


def test_distinct_intents_for_same_target_share_one_effect_attempt(tmp_path: Path) -> None:
    observed, manifest_path, first_intent, first_path = prepare_candidate_intent(tmp_path)
    authority_store = authority_store_for_intent(first_intent)
    second_intent, second_path = refresh.prepare_intent(
        candidate=observed,
        state_root=Path(first_intent["state_root"]),
        prefix=Path(first_intent["prefix"]),
        bin_dir=Path(first_intent["bin_dir"]),
        remote_url=first_intent["remote_url"],
        authorized_by="chatgpt",
        authorization="Second explicit authorization for the same exact target.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-2026-003-T009",
        now=NOW + timedelta(seconds=1),
        authority_store=authority_store,
    )
    assert first_intent["intent_sha256"] != second_intent["intent_sha256"]
    assert first_intent["target_sha256"] == second_intent["target_sha256"]
    binding, resource_db = lease_for(tmp_path / "leases", first_intent)
    effects = 0

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        nonlocal effects
        effects += 1
        kwargs["workspace"].mkdir(parents=True)
        return {"head": MAIN, "root": str(kwargs["workspace"])}

    def installer(**_: Any) -> dict[str, Any]:
        return {"manifest_sha256": "a" * 64}

    def readback(**_: Any) -> dict[str, Any]:
        return {"source_commit": MAIN, "check_valid": True}

    first = refresh.apply_runtime_refresh(
        intent_path=first_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=Path(first_intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: observed,
        source_preparer=source_preparer,
        installer=installer,
        readback=readback,
    )
    second = refresh.apply_runtime_refresh(
        intent_path=second_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=Path(first_intent["state_root"]),
        resource_db=resource_db,
        now=NOW + timedelta(seconds=1),
        observer=lambda **_: pytest.fail("target result must be reused before observation"),
        source_preparer=lambda **_: pytest.fail("source preparation must not repeat"),
        installer=lambda **_: pytest.fail("installer must not repeat"),
    )

    assert first["status"] == "deployed"
    assert second["status"] == "deployed"
    assert second["reused"] is True
    assert second["intent_sha256"] == first_intent["intent_sha256"]
    assert effects == 1


def test_apply_installer_failure_is_durable_unclear_and_never_retried(
    tmp_path: Path,
) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    calls = 0

    def observer(**_: Any) -> dict[str, Any]:
        return observed

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        kwargs["workspace"].mkdir(parents=True)
        return {"head": MAIN}

    def installer(**_: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise refresh.RuntimeRefreshError(
            "installer-returned-nonzero", "effect outcome is not established"
        )

    lease_binding, resource_db = lease_for(tmp_path / "leases", intent)
    first = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
        source_preparer=source_preparer,
        installer=installer,
    )
    second = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
        source_preparer=source_preparer,
        installer=installer,
    )

    assert first["status"] == "unclear"
    assert first["effect_started"] is True
    assert first["workspace_preserved"] is True
    assert second["status"] == "unclear"
    assert second["reused"] is True
    assert calls == 1


def test_apply_existing_start_without_result_is_unclear_without_execution(
    tmp_path: Path,
) -> None:
    _, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    attempt = Path(intent["state_root"]) / "attempts" / intent["target_sha256"]
    refresh.create_only(
        attempt / "started.json",
        refresh.canonical_bytes({"kind": "attempt", "effect_started": False}),
    )
    executed = False

    def observer(**_: Any) -> dict[str, Any]:
        nonlocal executed
        executed = True
        raise AssertionError("observer must not run")

    lease_binding, resource_db = lease_for(tmp_path / "leases", intent)
    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
    )

    assert result["status"] == "unclear_existing_attempt"
    assert result["reused"] is True
    assert executed is False


def test_apply_already_current_deduplicates_without_installer(tmp_path: Path) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    live = dict(observed)
    live.update(
        {
            "status": "already_current",
            "deployed_source_commit": MAIN,
            "main_commit": MAIN,
            "reason_codes": [],
        }
    )
    live = refresh.bind_digest(live, "observation_sha256")
    write_manifest(manifest_path, MAIN)

    lease_binding, resource_db = lease_for(tmp_path / "leases", intent)
    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: live,
        source_preparer=lambda **_: pytest.fail("source preparation must not run"),
        installer=lambda **_: pytest.fail("installer must not run"),
    )

    assert result["status"] == "already_current"
    assert result["effect_started"] is False


@pytest.mark.parametrize(
    "task_id",
    [
        "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811",
        "BUREAU-TRUTH-MODEL-V2-T029",
    ],
)
def test_no_run_closeout_is_receipt_bound_and_idempotent(tmp_path: Path, task_id: str) -> None:
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    assert "authority_state_store" not in intent
    assert "authority_task_spec" not in intent
    assert store.list_runs() == []
    release_test_leases(resource_db)

    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "verified"
    assert closeout["closeout"]["task_id"] == task_id
    assert closeout["closeout"]["runtime_result_sha256"] == result["result_sha256"]
    assert closeout["closeout"]["target_sha256"] == intent["target_sha256"]
    assert closeout["closeout"]["does_not_establish"] == [
        "retroactive_claim_authority",
        "synthetic_run_authority",
        "runtime_authority_for_later_targets",
        "future_runtime_health",
    ]
    terminal_revision = current["revision"]
    assert store.list_runs() == []

    replay = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=21),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert replay["idempotent_replay"] is True
    assert store.task_spec(task_id)["revision"] == terminal_revision
    assert store.list_runs() == []


def test_no_run_closeout_authenticates_historical_runtime_after_successor_deploy(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    install_receipt, historical_readback, artifacts = historical_runtime_artifacts(tmp_path, intent)
    result = replace_historical_result(
        intent,
        result,
        install_receipt=install_receipt,
        readback=historical_readback,
    )
    release_test_leases(resource_db)
    successor_manifest = artifacts["manifest"].read_bytes()

    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
    )

    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "verified"
    assert closeout["closeout"]["runtime_result_sha256"] == result["result_sha256"]
    assert artifacts["manifest"].read_bytes() == successor_manifest
    assert json.loads(successor_manifest)["source_commit"] == "9" * 40
    assert historical_readback["source_commit"] == intent["main_commit"]


@pytest.mark.parametrize(
    ("damage", "expected_code"),
    [
        ("manifest", "historical-runtime-manifest-evidence-unavailable"),
        ("launcher", "historical-runtime-launcher-evidence-unavailable"),
        ("release", "historical-runtime-release-invalid"),
        ("package", "historical-runtime-package-mismatch"),
        ("registry", "historical-runtime-registry-snapshot-invalid"),
        ("receipt", "historical-install-receipt-mismatch"),
    ],
)
def test_no_run_closeout_fails_closed_on_damaged_historical_runtime_evidence(
    tmp_path: Path,
    damage: str,
    expected_code: str,
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    install_receipt, historical_readback, artifacts = historical_runtime_artifacts(tmp_path, intent)
    result = replace_historical_result(
        intent,
        result,
        install_receipt=install_receipt,
        readback=historical_readback,
    )
    release_test_leases(resource_db)
    before = store.task_spec(task_id)
    assert before is not None

    if damage == "manifest":
        (artifacts["backup"] / "deployment-manifest.json").unlink()
    elif damage == "launcher":
        (artifacts["backup"] / "bureau").write_text("tampered\n", encoding="utf-8")
    elif damage == "release":
        shutil.rmtree(artifacts["release"])
    elif damage == "package":
        (artifacts["release"] / "src/bureau/runtime_identity.py").write_text(
            "TAMPERED = True\n", encoding="utf-8"
        )
    elif damage == "registry":
        artifacts["registry_task"].write_text('{"id":"TAMPERED"}\n', encoding="utf-8")
    else:
        persisted_receipt = json.loads(artifacts["receipt"].read_text(encoding="utf-8"))
        persisted_receipt["installed_at"] = refresh.isoformat(NOW + timedelta(seconds=1))
        artifacts["receipt"].write_bytes(refresh.canonical_bytes(persisted_receipt))

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
        )

    assert caught.value.code == expected_code
    assert store.task_spec(task_id) == before
    assert "runtime_closeout" not in before["spec"]["metadata"]


def test_no_run_closeout_rejects_historical_multi_use_of_single_use_authority(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    state_root = Path(intent["state_root"])
    second_intent = json.loads(json.dumps(intent))
    second_intent["target_sha256"] = "e" * 64
    second_intent["main_commit"] = "4" * 40
    second_intent["nonce"] = "historical-second-use"
    second_intent = refresh.bind_digest(second_intent, "intent_sha256")
    refresh.create_only(
        state_root / "intents" / f"{second_intent['intent_sha256']}.json",
        refresh.canonical_bytes(second_intent),
    )
    second_attempt = state_root / "attempts" / second_intent["target_sha256"]
    second_started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": second_intent["intent_sha256"],
            "target_sha256": second_intent["target_sha256"],
            "main_commit": second_intent["main_commit"],
            "lease_binding": result["lease_binding"],
            "started_at": refresh.isoformat(NOW + timedelta(minutes=1)),
            "effect_started": False,
        },
        "start_sha256",
    )
    refresh.create_only(second_attempt / "started.json", refresh.canonical_bytes(second_started))
    second_result = refresh._write_attempt_result(
        second_attempt / "result.json",
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": second_intent["intent_sha256"],
            "target_sha256": second_intent["target_sha256"],
            "main_commit": second_intent["main_commit"],
            "source_identity": {"head": second_intent["main_commit"]},
            "install_receipt": result["install_receipt"],
            "readback": {**result["readback"], "source_commit": second_intent["main_commit"]},
            "lease_binding": result["lease_binding"],
            "finished_at": refresh.isoformat(NOW + timedelta(minutes=1)),
            "effect_started": True,
        },
    )
    release_test_leases(resource_db)
    before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=state_root,
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-historical-multi-use"
    assert caught.value.details["conflicts"] == [
        {
            "intent_sha256": second_intent["intent_sha256"],
            "target_sha256": second_intent["target_sha256"],
            "result_sha256": second_result["result_sha256"],
            "status": "deployed",
            "main_commit": second_intent["main_commit"],
        }
    ]
    assert store.task_spec(task_id) == before
    assert store.list_runs() == []


def test_no_run_closeout_rejects_missing_release_and_wrong_or_tampered_evidence(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        intent,
        _,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path)
    revision_before = store.task_spec(intent["approval_task_id"])["revision"]

    with pytest.raises(refresh.RuntimeRefreshError) as live_lease_error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=intent["approval_task_id"],
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert live_lease_error.value.code == "authority-closeout-leases-not-released"
    assert store.task_spec(intent["approval_task_id"])["revision"] == revision_before

    release_test_leases(resource_db)
    with pytest.raises(refresh.RuntimeRefreshError) as task_error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id="BUR-WRONG-AUTHORITY",
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert task_error.value.code == "authority-closeout-intent-mismatch"

    with pytest.raises(refresh.RuntimeRefreshError) as target_error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=intent["approval_task_id"],
            target_sha256="f" * 64,
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert target_error.value.code == "authority-closeout-intent-mismatch"

    result_path = Path(intent["state_root"]) / "attempts" / intent["target_sha256"] / "result.json"
    tampered = dict(result)
    tampered["readback"] = {"source_commit": "f" * 40}
    result_path.write_bytes(refresh.canonical_bytes(tampered))
    with pytest.raises(refresh.RuntimeRefreshError) as result_error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=intent["approval_task_id"],
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert result_error.value.code == "digest-mismatch"
    assert store.task_spec(intent["approval_task_id"])["revision"] == revision_before


def test_no_run_closeout_rejects_conflicting_authoritative_terminal_state(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        intent,
        _,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path)
    release_test_leases(resource_db)
    revise_authority(
        store,
        intent["approval_task_id"],
        lambda spec: spec.__setitem__("state", "superseded"),
        key="conflicting-terminal-state",
    )
    before = store.task_spec(intent["approval_task_id"])

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=intent["approval_task_id"],
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert error.value.code == "authority-task-terminal"
    assert store.task_spec(intent["approval_task_id"]) == before
    assert "runtime_closeout" not in before["spec"]["metadata"]


def test_readback_validates_all_launchers_and_runtime_identity(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    manifest_path = prefix / "deployment-manifest.json"
    write_manifest(
        manifest_path,
        MAIN,
        release_id="release",
        package_tree_sha256="a" * 64,
        canonical_registry_tree_sha256="b" * 64,
    )
    bureau = bin_dir / "bureau"
    bureau.write_text(
        """#!/usr/bin/env python3
import json, sys
if sys.argv[-1] == 'check':
    print(json.dumps({'result': {'valid': True}}))
else:
    print(json.dumps({'result': {'status': 'ok'}, 'runtime_identity': {'manifest': {
        'valid': True,
        'source_commit': '"""
        + MAIN
        + """',
        'observed_package_tree_sha256': '"""
        + "a" * 64
        + """',
        'canonical_registry': {'valid': True, 'observed_tree_sha256': '"""
        + "b" * 64
        + """'}
    }}}))
""",
        encoding="utf-8",
    )
    bureau.chmod(0o755)
    runner = bin_dir / "bureau-runtime-refresh"
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    status_capsule = bin_dir / "bureau-status-capsule"
    status_capsule.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    status_capsule.chmod(0o755)
    receipt = {
        "manifest_sha256": refresh.sha256_bytes(manifest_path.read_bytes()),
        "launcher_sha256": refresh.sha256_bytes(bureau.read_bytes()),
        "runtime_refresh_launcher_sha256": refresh.sha256_bytes(runner.read_bytes()),
        "status_capsule_launcher_sha256": refresh.sha256_bytes(status_capsule.read_bytes()),
        "rollback": {"directory": "/rollback"},
    }

    result = refresh.readback_install(
        expected_commit=MAIN,
        prefix=prefix,
        bin_dir=bin_dir,
        install_receipt=receipt,
    )

    assert result["check_valid"] is True
    assert result["runtime_identity_valid"] is True
    assert result["source_commit"] == MAIN
    assert result["status_capsule_launcher_sha256"] == receipt["status_capsule_launcher_sha256"]
    assert result["rollback"] == {"directory": "/rollback"}

    status_capsule.unlink()
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.readback_install(
            expected_commit=MAIN,
            prefix=prefix,
            bin_dir=bin_dir,
            install_receipt=receipt,
        )
    assert missing.value.code == "readback-launcher-invalid"

    status_capsule.symlink_to(runner)
    with pytest.raises(refresh.RuntimeRefreshError) as symlinked:
        refresh.readback_install(
            expected_commit=MAIN,
            prefix=prefix,
            bin_dir=bin_dir,
            install_receipt=receipt,
        )
    assert symlinked.value.code == "readback-launcher-invalid"

    status_capsule.unlink()
    status_capsule.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    status_capsule.chmod(0o755)
    bad_receipt = {**receipt, "status_capsule_launcher_sha256": "0" * 64}
    with pytest.raises(refresh.RuntimeRefreshError) as mismatch:
        refresh.readback_install(
            expected_commit=MAIN,
            prefix=prefix,
            bin_dir=bin_dir,
            install_receipt=bad_receipt,
        )
    assert mismatch.value.code == "readback-launcher-mismatch"


def load_installer_module() -> Any:
    path = Path(__file__).parents[1] / "ops/install-bureau-runtime.py"
    spec = importlib.util.spec_from_file_location("install_bureau_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_wrapper_selects_refresh_entrypoint_and_backs_up_both(
    tmp_path: Path,
) -> None:
    installer = load_installer_module()
    rendered = installer.wrapper(
        tmp_path / "deployment-manifest.json",
        "bureau.runtime_refresh",
    ).decode()
    assert "importlib.import_module('bureau.runtime_refresh')" in rendered
    assert installer.MANAGED_MARKER in rendered
    assert "manifest payload digest mismatch" in rendered

    prefix = tmp_path / "prefix"
    manifest = prefix / "deployment-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("manifest", encoding="utf-8")
    primary = tmp_path / "bin/bureau"
    secondary = tmp_path / "bin/bureau-runtime-refresh"
    primary.parent.mkdir()
    primary.write_text("primary", encoding="utf-8")
    secondary.write_text("secondary", encoding="utf-8")

    backup = installer._backup_existing(prefix, manifest, primary, secondary)

    assert Path(backup["manifest"]).read_text(encoding="utf-8") == "manifest"
    assert Path(backup["launcher"]).read_text(encoding="utf-8") == "primary"
    assert Path(backup["runtime_refresh_launcher"]).read_text(encoding="utf-8") == "secondary"


def test_status_reports_terminal_and_unresolved_attempts(tmp_path: Path) -> None:
    manifest = tmp_path / "prefix/deployment-manifest.json"
    write_manifest(manifest)
    state = tmp_path / "state"
    terminal = refresh.bind_digest(
        {
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
        },
        "result_sha256",
    )
    refresh.create_only(
        state / "attempts/terminal/result.json",
        refresh.canonical_bytes(terminal),
    )
    refresh.create_only(
        state / "attempts/unresolved/started.json",
        refresh.canonical_bytes({"kind": "start"}),
    )

    result = refresh.status_report(state, manifest)

    assert {item["status"] for item in result["attempts"]} == {
        "deployed",
        "unclear_existing_attempt",
    }


def test_real_installer_publishes_working_refresh_launcher(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    staged = tmp_path / "staged"
    shutil.copytree(
        repository,
        staged,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "*.pyc",
        ),
    )
    git(staged, "init", "-b", "main")
    git(staged, "config", "user.name", "Test")
    git(staged, "config", "user.email", "test@example.invalid")
    git(staged, "add", ".")
    git(staged, "commit", "-m", "synthetic T016 source")
    bare = tmp_path / "bureau.git"
    git(tmp_path, "clone", "--bare", str(staged), str(bare))
    clean = tmp_path / "clean"
    git(tmp_path, "clone", str(bare), str(clean))
    git(clean, "config", "user.name", "Test")
    git(clean, "config", "user.email", "test@example.invalid")
    git(clean, "remote", "set-url", "origin", str(bare))
    git(clean, "fetch", "origin", "main")

    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    command = [
        sys.executable,
        str(clean / "ops/install-bureau-runtime.py"),
        "--source",
        str(clean),
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bin_dir),
    ]
    denied = subprocess.run(
        command,
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert denied.returncode != 0
    assert "runtime approval denied" in denied.stderr
    assert not prefix.exists()

    expired_approval = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="expired-installer",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    expired = subprocess.run(
        [*command, "--approval-intent", str(expired_approval)],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert expired.returncode != 0
    assert "intent-expired" in expired.stderr
    assert not prefix.exists()

    approval_path = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="real-installer",
    )

    install = subprocess.run(
        [*command, "--approval-intent", str(approval_path)],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert install.returncode == 0, install.stderr
    receipt = json.loads(install.stdout.strip().splitlines()[-1])
    bureau = bin_dir / "bureau"
    runner = bin_dir / "bureau-runtime-refresh"

    assert bureau.is_file() and os.access(bureau, os.X_OK)
    assert runner.is_file() and os.access(runner, os.X_OK)
    assert receipt["launcher_sha256"] == refresh.sha256_bytes(bureau.read_bytes())
    assert receipt["runtime_refresh_launcher_sha256"] == refresh.sha256_bytes(runner.read_bytes())

    check = subprocess.run(
        [str(bureau), "--json", "check"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(check.stdout)["result"]["valid"] is True
    status = subprocess.run(
        [
            str(runner),
            "--state-root",
            str(tmp_path / "refresh-state"),
            "status",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(status.stdout)
    assert payload["kind"] == "bureau_runtime_refresh_status"
    deployed_head = git(clean, "rev-parse", "HEAD")
    assert payload["deployed_source_commit"] == deployed_head

    launcher_hashes = {
        path.name: refresh.sha256_bytes(path.read_bytes())
        for path in (bureau, runner, bin_dir / "bureau-status-capsule")
    }
    repeated_approval = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="stable-launcher-repeat",
    )
    repeated = subprocess.run(
        [
            *command,
            "--approval-intent",
            str(repeated_approval),
            "--replace-existing",
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert repeated.returncode == 0, repeated.stderr
    repeated_receipt = json.loads(repeated.stdout.strip().splitlines()[-1])
    assert repeated_receipt["launcher_written"] is False
    assert repeated_receipt["runtime_refresh_launcher_written"] is False
    assert repeated_receipt["status_capsule_launcher_written"] is False
    assert launcher_hashes == {
        path.name: refresh.sha256_bytes(path.read_bytes())
        for path in (bureau, runner, bin_dir / "bureau-status-capsule")
    }

    status_capsule = bin_dir / "bureau-status-capsule"
    stable_status_capsule = status_capsule.read_bytes()
    manifest_before_launcher_drift = (prefix / "deployment-manifest.json").read_bytes()
    drift_approval = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="stable-launcher-drift",
    )
    status_capsule.write_text("drifted launcher\n", encoding="utf-8")
    drift_blocked = subprocess.run(
        [
            *command,
            "--approval-intent",
            str(drift_approval),
            "--replace-existing",
            "--enforce-launcher-allowlist",
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert drift_blocked.returncode != 0
    assert "launcher mutation is not covered" in drift_blocked.stderr
    assert (prefix / "deployment-manifest.json").read_bytes() == manifest_before_launcher_drift
    assert status_capsule.read_text(encoding="utf-8") == "drifted launcher\n"
    status_capsule.write_bytes(stable_status_capsule)
    status_capsule.chmod(0o755)

    symlink_target = tmp_path / "status-capsule-symlink-target"
    symlink_target.write_text("legacy symlink target\n", encoding="utf-8")
    status_capsule.unlink()
    status_capsule.symlink_to(symlink_target)
    symlink_approval = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="stable-launcher-symlink-repair",
    )
    symlink_repair = subprocess.run(
        [
            *command,
            "--approval-intent",
            str(symlink_approval),
            "--replace-existing",
            "--enforce-launcher-allowlist",
            "--allowed-launcher-path",
            str(status_capsule),
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert symlink_repair.returncode == 0, symlink_repair.stderr
    symlink_receipt = json.loads(symlink_repair.stdout.strip().splitlines()[-1])
    assert symlink_receipt["launcher_written"] is False
    assert symlink_receipt["runtime_refresh_launcher_written"] is False
    assert symlink_receipt["status_capsule_launcher_written"] is True
    assert status_capsule.is_file() and not status_capsule.is_symlink()
    assert status_capsule.read_bytes() == stable_status_capsule

    manifest_payload = json.loads((prefix / "deployment-manifest.json").read_text())
    manifest_payload["source_commit"] = "f" * 40
    (prefix / "deployment-manifest.json").write_bytes(refresh.canonical_bytes(manifest_payload))
    tampered = subprocess.run(
        [str(bureau), "--json", "check"],
        check=False,
        text=True,
        capture_output=True,
    )
    assert tampered.returncode != 0
    assert "manifest payload digest mismatch" in tampered.stderr
    repeated = subprocess.run(
        [
            *command,
            "--approval-intent",
            str(repeated_approval),
            "--replace-existing",
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert repeated.returncode == 0, repeated.stderr

    readme = clean / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nLegacy refresh cutover fixture.\n",
        encoding="utf-8",
    )
    git(clean, "add", "README.md")
    git(clean, "commit", "-m", "synthetic legacy cutover target")
    target_head = git(clean, "rev-parse", "HEAD")
    git(clean, "update-ref", "refs/remotes/origin/main", target_head)
    assert target_head != deployed_head
    deployed_manifest, deployed_manifest_sha = refresh.load_manifest(
        prefix / "deployment-manifest.json"
    )
    current = datetime.now(timezone.utc)
    legacy_state = (tmp_path / "legacy-refresh-state").resolve()
    workspace = legacy_state / "workspaces" / target_head
    target_sha = refresh.sha256_bytes(
        refresh.canonical_bytes(
            {
                "kind": "legacy-cutover-test-target",
                "source_commit": target_head,
                "deployed_manifest_sha256": deployed_manifest_sha,
            }
        )
    )
    legacy_intent = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_intent",
            "repository": "heimgewebe/bureau",
            "remote_url": str(bare),
            "main_commit": target_head,
            "pull_request": 1236,
            "merged_at": refresh.isoformat(current),
            "required_checks": list(refresh.DEFAULT_REQUIRED_CHECKS),
            "target_sha256": target_sha,
            "observation_sha256": "b" * 64,
            "expected_deployed_source_commit": deployed_manifest["source_commit"],
            "expected_manifest_sha256": deployed_manifest_sha,
            "state_root": str(legacy_state),
            "prefix": str(prefix.resolve()),
            "bin_dir": str(bin_dir.resolve()),
            "workspace": str(workspace),
            "required_resource_keys": refresh.required_resource_keys(
                state_root=legacy_state,
                prefix=prefix.resolve(),
                bin_dir=bin_dir.resolve(),
                workspace=workspace,
            ),
            "authorized_by": "legacy-runner-test",
            "authorization": "Explicit operator authorization from the old runner.",
            "created_at": refresh.isoformat(current),
            "expires_at": refresh.isoformat(current + timedelta(minutes=15)),
            "nonce": "legacy-installer-cutover",
            "does_not_establish": [
                "external_lease_liveness",
                "merge_authority",
                "automatic_retry_authority",
            ],
        },
        "intent_sha256",
    )
    legacy_intent_path = legacy_state / "intents" / f"{legacy_intent['intent_sha256']}.json"
    refresh.create_only(legacy_intent_path, refresh.canonical_bytes(legacy_intent))
    legacy_binding, legacy_resource_db = lease_for(
        tmp_path / "legacy-installer-leases",
        legacy_intent,
        current=current,
    )
    normalized_binding = refresh.validate_live_lease_binding(
        legacy_intent,
        legacy_binding,
        resource_db=legacy_resource_db,
        now=current,
    )
    legacy_started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": legacy_intent["intent_sha256"],
            "target_sha256": legacy_intent["target_sha256"],
            "main_commit": target_head,
            "lease_binding": normalized_binding,
            "started_at": refresh.isoformat(current),
            "effect_started": False,
        },
        "start_sha256",
    )
    refresh.create_only(
        legacy_state / "attempts" / target_sha / "started.json",
        refresh.canonical_bytes(legacy_started),
    )

    legacy_install = subprocess.run(
        [
            *command,
            "--runtime-refresh-state-root",
            str(legacy_state),
            "--resource-db",
            str(legacy_resource_db),
            "--replace-existing",
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert legacy_install.returncode == 0, legacy_install.stderr
    legacy_receipt = json.loads(legacy_install.stdout.strip().splitlines()[-1])
    assert legacy_receipt["launcher_written"] is False
    assert legacy_receipt["runtime_refresh_launcher_written"] is False
    assert legacy_receipt["status_capsule_launcher_written"] is False
    assert launcher_hashes == {
        path.name: refresh.sha256_bytes(path.read_bytes())
        for path in (bureau, runner, status_capsule)
    }
    assert legacy_receipt["runtime_approval"]["required_level"] == "legacy_runtime_operator_gate"
    legacy_cutover = legacy_receipt["runtime_approval"]["legacy_cutover"]
    assert legacy_cutover["intent_sha256"] == legacy_intent["intent_sha256"]
    assert legacy_cutover["start_sha256"] == legacy_started["start_sha256"]
    assert legacy_cutover["expected_source_commit"] == target_head
    live_binding_sha256 = legacy_cutover["lease_binding_sha256"]
    assert isinstance(live_binding_sha256, str)
    assert len(live_binding_sha256) == 64
    assert all(character in "0123456789abcdef" for character in live_binding_sha256)
    upgraded_status = subprocess.run(
        [str(runner), "--state-root", str(legacy_state), "status"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(upgraded_status.stdout)["deployed_source_commit"] == target_head
