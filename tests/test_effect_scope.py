from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from bureau import cli as bureau_cli
from bureau import effect_scope
from bureau.core import Dispatcher, Registry, StateStore
from bureau.registry_snapshot import snapshot_tree_sha256


def canonical_runtime_identity(
    root: Path,
    *,
    commit: str = "a" * 40,
    tree_sha256: str = "b" * 64,
) -> dict:
    resolved = root.resolve()
    return {
        "schema_version": 1,
        "kind": "bureau_runtime_identity",
        "registry": {
            "available": True,
            "bureau_project": True,
            "role": "canonical-runtime-snapshot",
            "root": str(resolved),
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
                "root": str(resolved),
                "source_commit": commit,
                "tree_sha256": tree_sha256,
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


def registry_file_evidence(root: Path) -> dict:
    paths = sorted(
        path.relative_to(root)
        for path in (root / "registry").rglob("*")
        if path.is_file()
    )
    files = {}
    for relative in paths:
        content = (root / relative).read_bytes()
        files[relative.as_posix()] = {
            "bytes": content,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    tree_sha256 = snapshot_tree_sha256(root, paths)
    assert tree_sha256 is not None
    return {"files": files, "tree_sha256": tree_sha256}


def run_events(store: StateStore, run_id: str) -> list[dict]:
    with store.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT event_id,event_type,event_schema_version,payload_json,created_at "
                "FROM events WHERE run_id=? ORDER BY event_id",
                (run_id,),
            )
        ]


def test_command_effect_scope_is_explicit_and_fails_closed() -> None:
    assert effect_scope.classify_command_effect_scope("status", mutates=False) == "read_only"
    assert (
        effect_scope.classify_command_effect_scope("bind", mutates=True)
        == "coordination_state_mutation"
    )
    assert (
        effect_scope.classify_command_effect_scope("claim-commit", mutates=True)
        == "coordination_state_mutation"
    )
    assert (
        effect_scope.classify_command_effect_scope("lifecycle-reconcile-apply", mutates=True)
        == "coordination_state_mutation"
    )
    assert (
        effect_scope.classify_command_effect_scope("future-command", mutates=True)
        == "registry_mutation"
    )
    assess = bureau_cli.parser().parse_args(["projection-repair", "--assess"])
    apply = bureau_cli.parser().parse_args(["projection-repair", "--apply"])
    assert bureau_cli._command_mutates(assess) is False
    assert bureau_cli._command_effect_scope(assess) == "read_only"
    assert bureau_cli._command_mutates(apply) is True
    assert bureau_cli._command_effect_scope(apply) == "coordination_state_mutation"
    unknown = SimpleNamespace(command="future-command")
    assert bureau_cli._command_mutates(unknown) is True
    assert bureau_cli._command_effect_scope(unknown) == "registry_mutation"


def test_lifecycle_reconcile_apply_uses_canonical_coordination_store(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    task_path = next((registry_root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "blocked"
    task_path.write_text(json.dumps(task))
    queue_path = registry_root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"]["now"] = []
    queue_path.write_text(json.dumps(queue))
    state_root = tmp_path / "coordination-state"
    store = StateStore(state_root / "bureau.sqlite3")
    store.import_registry_task_specs(Registry.load(registry_root))
    identity = canonical_runtime_identity(registry_root)
    initiative_path = registry_root / "registry/initiatives/main.json"
    initiative_before = initiative_path.read_bytes()
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)

    result = bureau_cli.main(
        [
            "--state-root", str(state_root),
            "--json", "lifecycle-reconcile-apply",
        ]
    )

    assert result == 0
    value = json.loads(capsys.readouterr().out)
    assert value["runtime_identity"]["command_effect_scope"] == "coordination_state_mutation"
    result = value["result"]
    assert result["changed_count"] == 1
    assert result["changed"][0]["to_state"] == "waiting"
    assert (state_root / "bureau.sqlite3").is_file()
    assert initiative_path.read_bytes() == initiative_before


def test_claim_intent_is_coordination_state_mutation() -> None:
    assert bureau_cli._command_mutates(
        bureau_cli.parser().parse_args(["claim-intent", "--worker", "test-worker"])
    )
    assert (
        effect_scope.classify_command_effect_scope("claim-intent", mutates=True)
        == "coordination_state_mutation"
    )



def test_claim_intent_readback_is_read_only() -> None:
    args = bureau_cli.parser().parse_args(
        ["claim-intent-readback", "--idempotency-key", "request-1"]
    )
    assert bureau_cli._command_mutates(args) is False
    assert bureau_cli._command_effect_scope(args) == "read_only"

def test_canonical_claim_intent_uses_writable_coordination_store(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    state_root = tmp_path / "coordination-state"
    identity = canonical_runtime_identity(registry_root)

    class FakeDispatcher:
        def __init__(
            self,
            registry,
            store,
            adapter_registry,
            enforce_runtime_gate=True,
            runtime_identity=None,
        ):
            self.store = store

        def claim_intent(self, worker, capabilities, kind, **kwargs):
            return {
                "status": "claim-intent",
                "state_db": str(self.store.path),
                "store_type": type(self.store).__name__,
            }

    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)
    monkeypatch.setattr(bureau_cli, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())
    result = bureau_cli.main(
        [
            "--root", str(registry_root),
            "--state-root", str(state_root),
            "--json", "claim-intent",
            "--worker", "test-worker",
        ]
    )
    assert result == 0
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "claim-intent"
    assert value["result"]["store_type"] == "StateStore"
    assert value["result"]["state_db"] == str(state_root / "bureau.sqlite3")
    assert value["runtime_identity"]["command_effect_scope"] == "coordination_state_mutation"
    assert (state_root / "bureau.sqlite3").is_file()


def test_canonical_coordination_binding_requires_separate_absolute_state_root(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    identity = canonical_runtime_identity(registry_root)
    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=None,
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )
    assert binding is None
    assert blocked["status"] == "explicit-coordination-state-root-required"
    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value="relative-state",
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )
    assert binding is None
    assert blocked["status"] == "coordination-state-path-invalid"
    assert "coordination-state-path-not-absolute" in blocked["reason_codes"]


def test_canonical_coordination_binding_rejects_registry_overlap(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    identity = canonical_runtime_identity(registry_root)
    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=str(registry_root / "state"),
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )
    assert binding is None
    assert blocked["status"] == "coordination-state-path-invalid"
    assert "coordination-state-root-overlaps-registry" in blocked["reason_codes"]


def test_canonical_coordination_binding_accepts_owner_controlled_external_root(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    state_root = tmp_path / "state"
    identity = canonical_runtime_identity(registry_root)
    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=str(state_root),
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )
    assert blocked is None
    assert binding["registry_root"] == str(registry_root.resolve())
    assert binding["state_root"] == str(state_root.resolve())
    assert binding["state_db"] == str((state_root / "bureau.sqlite3").resolve())
    assert binding["registry_source_commit"] == "a" * 40


def test_canonical_coordination_binding_rejects_hard_link_to_file_outside_root(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    state_db = state_root / "bureau.sqlite3"
    outside_db = tmp_path / "outside.sqlite3"
    outside_bytes = b"outside database must remain unchanged"
    outside_db.write_bytes(outside_bytes)
    outside_inode = outside_db.stat().st_ino
    os.link(outside_db, state_db)
    identity = canonical_runtime_identity(registry_root)

    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=str(state_root),
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )

    assert binding is None
    assert blocked["status"] == "coordination-state-path-invalid"
    assert blocked["reason_codes"] == ["coordination-state-db-hardlink-ambiguous"]
    assert outside_db.read_bytes() == outside_bytes
    assert outside_db.stat().st_ino == outside_inode == state_db.stat().st_ino
    assert outside_db.stat().st_nlink == 2


def test_canonical_coordination_binding_accepts_existing_single_link_database(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    state_root = tmp_path / "state"
    state_db = state_root / "bureau.sqlite3"
    StateStore(state_db)
    assert state_db.stat().st_nlink == 1
    identity = canonical_runtime_identity(registry_root)

    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=str(state_root),
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )

    assert blocked is None
    assert binding is not None
    assert binding["state_db"] == str(state_db)
    assert binding["state_db_existed_at_binding"] is True


def test_canonical_coordination_binding_accepts_missing_sidecar_directories(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    identity = canonical_runtime_identity(registry_root)

    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=str(state_root),
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )

    assert blocked is None
    assert binding is not None
    assert not (state_root / "envelopes").exists()
    assert not (state_root / "receipts").exists()


def test_canonical_coordination_binding_rejects_envelopes_symlink_into_registry(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "envelopes").symlink_to(
        registry_root, target_is_directory=True
    )
    identity = canonical_runtime_identity(registry_root)

    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=str(state_root),
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )

    assert binding is None
    assert blocked["status"] == "coordination-state-path-invalid"
    assert "coordination-state-envelopes-symlink-component" in blocked["reason_codes"]


def test_canonical_coordination_binding_rejects_sidecar_with_wrong_type(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "receipts").write_text("not a directory", encoding="utf-8")
    identity = canonical_runtime_identity(registry_root)

    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=str(state_root),
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )

    assert binding is None
    assert blocked["status"] == "coordination-state-path-invalid"
    assert "coordination-state-receipts-not-directory" in blocked["reason_codes"]


def test_canonical_claim_commit_uses_separate_state_store(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    state_root = tmp_path / "coordination-state"
    identity = canonical_runtime_identity(registry_root)

    class FakeDispatcher:
        def __init__(
            self,
            registry,
            store,
            adapter_registry,
            enforce_runtime_gate=True,
            runtime_identity=None,
        ):
            self.store = store

        def commit_claim_intent(self, intent, lease_binding, *, resource_db):
            return {
                "status": "claimed",
                "state_db": str(self.store.path),
                "intent": intent,
            }

    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)
    monkeypatch.setattr(bureau_cli, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())
    monkeypatch.setattr(
        bureau_cli,
        "read_json_object_file",
        lambda path, field: {"run_id": "BUR-RUN-TEST"},
    )
    result = bureau_cli.main(
        [
            "--root", str(registry_root),
            "--state-root", str(state_root),
            "--json", "claim-commit",
            "--intent", str(tmp_path / "intent.json"),
        ]
    )
    assert result == 0
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "claimed"
    assert value["result"]["state_db"] == str(state_root / "bureau.sqlite3")
    assert value["runtime_identity"]["command_effect_scope"] == "coordination_state_mutation"
    assert value["runtime_identity"]["coordination_state_binding"]["state_root"] == str(state_root)
    assert (state_root / "bureau.sqlite3").is_file()


def test_canonical_claim_commit_without_state_root_is_blocked_before_effect(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    default_state = tmp_path / "default-state"
    identity = canonical_runtime_identity(registry_root)
    monkeypatch.setenv("BUREAU_STATE_DIR", str(default_state))
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)
    result = bureau_cli.main(
        [
            "--root", str(registry_root),
            "--json", "claim-commit",
            "--intent", str(tmp_path / "intent.json"),
        ]
    )
    assert result == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "explicit-coordination-state-root-required"
    assert not default_state.exists()


def test_canonical_doctor_repair_without_state_root_is_blocked_before_effect(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    default_state = tmp_path / "default-state"
    registry_before = registry_file_evidence(registry_root)
    identity = canonical_runtime_identity(registry_root)
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setenv("BUREAU_STATE_DIR", str(default_state))
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)

    exit_code = bureau_cli.main(["--json", "doctor", "--repair"])

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "explicit-coordination-state-root-required"
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )
    assert not default_state.exists()
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_doctor_repair_accepts_explicit_safe_state_root(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    state_root = tmp_path / "coordination-state"
    implicit_state_root = tmp_path / "implicit-state"
    state_db = state_root / "bureau.sqlite3"
    store = StateStore(state_db)
    run = Dispatcher(Registry.load(registry_root), store).claim_next(
        "doctor-worker", ("repository",), reconcile_first=False
    )["run"]
    envelope_path = store.envelope_path(run["run_id"])
    envelope_path.unlink()
    registry_before = registry_file_evidence(registry_root)
    identity = canonical_runtime_identity(registry_root)
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setenv("BUREAU_STATE_DIR", str(implicit_state_root))
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)
    monkeypatch.setattr(
        bureau_cli,
        "adapters",
        lambda args: SimpleNamespace(status=lambda: {}),
    )

    exit_code = bureau_cli.main(
        ["--state-root", str(state_root), "--json", "doctor", "--repair"]
    )

    assert exit_code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["repaired"] is True
    runtime = value["runtime_identity"]
    assert runtime["command_effect_scope"] == "coordination_state_mutation"
    assert runtime["coordination_state_binding"]["state_root"] == str(state_root)
    assert runtime["coordination_state_binding"]["state_db"] == str(state_db)
    assert state_db.is_file()
    assert envelope_path.is_file()
    assert not implicit_state_root.exists()
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_doctor_repair_recheck_rejects_receipts_symlink_outside_root(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    state_root = tmp_path / "coordination-state"
    state_db = state_root / "bureau.sqlite3"
    outside_root = tmp_path / "outside-state"
    outside_root.mkdir()
    registry_before = registry_file_evidence(registry_root)
    identity_calls = 0

    def runtime_identity(*args, **kwargs) -> dict:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 2:
            state_root.mkdir()
            (state_root / "receipts").symlink_to(
                outside_root, target_is_directory=True
            )
        return canonical_runtime_identity(registry_root)

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", runtime_identity)
    monkeypatch.setattr(
        bureau_cli,
        "adapters",
        lambda args: SimpleNamespace(status=lambda: {}),
    )

    exit_code = bureau_cli.main(
        ["--state-root", str(state_root), "--json", "doctor", "--repair"]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "coordination-state-path-invalid"
    assert (
        "coordination-state-receipts-symlink-component"
        in value["result"]["reason_codes"]
    )
    assert identity_calls == 2
    assert not state_db.exists()
    assert not (state_root / "envelopes").exists()
    assert (state_root / "receipts").is_symlink()
    assert list(outside_root.iterdir()) == []
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_doctor_repair_recheck_rejects_hard_link_before_store_open(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    state_root = tmp_path / "coordination-state"
    state_db = state_root / "bureau.sqlite3"
    outside_db = tmp_path / "outside.sqlite3"
    outside_bytes = b"foreign database must not be opened or mutated"
    outside_db.write_bytes(outside_bytes)
    outside_inode = outside_db.stat().st_ino
    registry_before = registry_file_evidence(registry_root)
    identity_calls = 0
    state_store_calls = 0

    def runtime_identity(*args, **kwargs) -> dict:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 2:
            state_root.mkdir()
            os.link(outside_db, state_db)
        return canonical_runtime_identity(registry_root)

    def unexpected_state_store(*args, **kwargs):
        nonlocal state_store_calls
        state_store_calls += 1
        raise AssertionError("StateStore must not open a hard-linked database")

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", runtime_identity)
    monkeypatch.setattr(bureau_cli, "StateStore", unexpected_state_store)
    monkeypatch.setattr(
        bureau_cli,
        "adapters",
        lambda args: SimpleNamespace(status=lambda: {}),
    )

    exit_code = bureau_cli.main(
        ["--state-root", str(state_root), "--json", "doctor", "--repair"]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "coordination-state-path-invalid"
    assert value["result"]["reason_codes"] == [
        "coordination-state-db-hardlink-ambiguous"
    ]
    assert identity_calls == 2
    assert state_store_calls == 0
    assert outside_db.read_bytes() == outside_bytes
    assert outside_db.stat().st_ino == outside_inode == state_db.stat().st_ino
    assert outside_db.stat().st_nlink == 2
    assert not (state_root / "envelopes").exists()
    assert not (state_root / "receipts").exists()
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_registry_mutation_remains_blocked_with_separate_state_root(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    identity = canonical_runtime_identity(registry_root)
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)
    result = bureau_cli.main(
        [
            "--root", str(registry_root),
            "--state-root", str(tmp_path / "state"),
            "--json", "close-ready",
        ]
    )
    assert result == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "stale-runtime-blocked"
    assert value["runtime_identity"]["command_effect_scope"] == "registry_mutation"
    assert not (tmp_path / "state").exists()


def test_canonical_coordination_binding_rejects_unowned_ancestor(
    tmp_path: Path, monkeypatch
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    identity = canonical_runtime_identity(registry_root)
    monkeypatch.setattr(effect_scope.os, "geteuid", lambda: -1)
    blocked, binding = effect_scope.canonical_coordination_state_binding(
        state_root_value=str(tmp_path / "new-state"),
        state_db_value=None,
        registry_root=registry_root,
        runtime_identity=identity,
    )
    assert binding is None
    assert "coordination-state-ancestor-owner-mismatch" in blocked["reason_codes"]


def test_canonical_claim_commit_rechecks_runtime_identity_before_store_open(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    state_root = tmp_path / "coordination-state"
    first = canonical_runtime_identity(registry_root)
    second = canonical_runtime_identity(registry_root)
    second["manifest"]["valid"] = False
    identities = iter([first, second])
    monkeypatch.setattr(
        bureau_cli, "bureau_runtime_identity", lambda *a, **k: next(identities)
    )
    result = bureau_cli.main(
        [
            "--root", str(registry_root),
            "--state-root", str(state_root),
            "--json", "claim-commit",
            "--intent", str(tmp_path / "intent.json"),
        ]
    )
    assert result == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "coordination-runtime-identity-blocked"
    assert "runtime-manifest-invalid" in value["result"]["reason_codes"]
    assert not state_root.exists()


def test_heartbeat_is_coordination_state_mutation() -> None:
    args = bureau_cli.parser().parse_args(
        ["heartbeat", "BUR-RUN-TEST", "--worker", "worker-a"]
    )

    assert bureau_cli._command_mutates(args) is True
    assert bureau_cli._command_effect_scope(args) == "coordination_state_mutation"
    assert (
        effect_scope.classify_command_effect_scope("heartbeat", mutates=True)
        == "coordination_state_mutation"
    )


def test_canonical_heartbeat_uses_explicit_coordination_state_store(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    from bureau.core import Dispatcher

    registry_root = registry_factory(1)
    state_root = tmp_path / "coordination-state"
    registry = Registry.load(registry_root)
    store = StateStore(state_root / "bureau.sqlite3")
    run = Dispatcher(registry, store).claim_next(
        "worker-a", ("repository",)
    )["run"]
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=?",
            (run["run_id"],),
        )
    identity = canonical_runtime_identity(registry_root)
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)

    exit_code = bureau_cli.main(
        [
            "--root", str(registry_root),
            "--state-root", str(state_root),
            "--json", "heartbeat", run["run_id"],
            "--worker", "worker-a",
        ]
    )

    assert exit_code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["run_id"] == run["run_id"]
    assert value["result"]["worker_id"] == "worker-a"
    assert value["result"]["heartbeat_at"] != "2000-01-01T00:00:00Z"
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )
    assert value["runtime_identity"]["coordination_state_binding"]["state_root"] == str(
        state_root
    )


def test_canonical_heartbeat_without_explicit_state_root_stops_before_effect(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    implicit_state_root = tmp_path / "implicit-state"
    identity = canonical_runtime_identity(registry_root)
    monkeypatch.setenv("BUREAU_STATE_DIR", str(implicit_state_root))
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: identity)

    exit_code = bureau_cli.main(
        [
            "--root", str(registry_root),
            "--json", "heartbeat", "BUR-RUN-TEST",
            "--worker", "worker-a",
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "explicit-coordination-state-root-required"
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )
    assert not implicit_state_root.exists()


def test_canonical_heartbeat_rechecks_runtime_identity_before_store_open(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    state_root = tmp_path / "coordination-state"
    first = canonical_runtime_identity(registry_root)
    second = canonical_runtime_identity(registry_root)
    second["manifest"]["valid"] = False
    identities = iter([first, second])
    monkeypatch.setattr(
        bureau_cli, "bureau_runtime_identity", lambda *a, **k: next(identities)
    )

    exit_code = bureau_cli.main(
        [
            "--root", str(registry_root),
            "--state-root", str(state_root),
            "--json", "heartbeat", "BUR-RUN-TEST",
            "--worker", "worker-a",
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "coordination-runtime-identity-blocked"
    assert "runtime-manifest-invalid" in value["result"]["reason_codes"]
    assert not state_root.exists()


def test_bind_is_coordination_state_mutation_in_cli_runtime_identity() -> None:
    args = bureau_cli.parser().parse_args(
        [
            "bind",
            "BUR-RUN-TEST",
            "--system",
            "codex",
            "--external-id",
            "session-test",
        ]
    )

    assert bureau_cli._command_mutates(args) is True
    assert bureau_cli._command_effect_scope(args) == "coordination_state_mutation"


def test_canonical_bind_mutates_only_revision_bound_coordination_state(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    state_root = tmp_path / "coordination-state"
    state_db = state_root / "bureau.sqlite3"
    registry = Registry.load(registry_root)
    store = StateStore(state_db)
    run = Dispatcher(registry, store).claim_next(
        "bind-worker", ("repository",), reconcile_first=False
    )["run"]
    run_before = store.run(run["run_id"])
    events_before = run_events(store, run["run_id"])
    registry_before = registry_file_evidence(registry_root)
    source_commit = registry_before["tree_sha256"][:40]
    identity_calls: list[tuple[Path, Path]] = []

    def runtime_identity(root: Path, *, state_path: Path) -> dict:
        identity_calls.append((root.resolve(), state_path))
        return canonical_runtime_identity(
            registry_root,
            commit=source_commit,
            tree_sha256=registry_before["tree_sha256"],
        )

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", runtime_identity)
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())

    exit_code = bureau_cli.main(
        [
            "--state-root",
            str(state_root),
            "--json",
            "bind",
            run["run_id"],
            "--system",
            "codex",
            "--external-id",
            "session-test",
        ]
    )

    assert exit_code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["run_id"] == run["run_id"]
    assert value["result"]["state"] == "running"
    assert value["result"]["external_system"] == "codex"
    assert value["result"]["external_id"] == "session-test"
    assert value["result"]["external_state"] == "running"
    runtime = value["runtime_identity"]
    assert runtime["command_effect_scope"] == "coordination_state_mutation"
    binding = runtime["coordination_state_binding"]
    assert binding["registry_source_commit"] == source_commit
    assert binding["registry_tree_sha256"] == registry_before["tree_sha256"]
    assert binding["state_root"] == str(state_root)
    assert binding["state_db"] == str(state_db)
    assert identity_calls == [
        (registry_root.resolve(), state_db),
        (registry_root.resolve(), state_db),
    ]

    run_after = store.run(run["run_id"])
    changed_run_fields = {
        key for key in run_before if run_before[key] != run_after[key]
    }
    assert {
        "state",
        "external_system",
        "external_id",
        "external_state",
        "external_observed_at",
    } <= changed_run_fields
    assert changed_run_fields <= {
        "state",
        "external_system",
        "external_id",
        "external_state",
        "external_observed_at",
        "updated_at",
        "heartbeat_at",
    }
    events_after = run_events(store, run["run_id"])
    assert [event["event_type"] for event in events_after[len(events_before) :]] == [
        "external-bound",
        "state-projection-v1",
    ]
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_bind_unknown_run_fails_without_registry_or_binding_effect(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    state_root = tmp_path / "coordination-state"
    store = StateStore(state_root / "bureau.sqlite3")
    registry_before = registry_file_evidence(registry_root)

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: canonical_runtime_identity(registry_root),
    )
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())

    exit_code = bureau_cli.main(
        [
            "--state-root",
            str(state_root),
            "--json",
            "bind",
            "BUR-RUN-MISSING",
            "--system",
            "codex",
            "--external-id",
            "session-missing",
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "failed"
    assert value["result"]["command"] == "bind"
    assert value["result"]["detail"] == "run BUR-RUN-MISSING is not active"
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )
    assert store.list_runs() == []
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='external-bound'"
        ).fetchone()[0] == 0
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_bind_rejects_conflicting_external_bindings_without_effect(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    state_root = tmp_path / "coordination-state"
    registry = Registry.load(registry_root)
    store = StateStore(state_root / "bureau.sqlite3")
    run = Dispatcher(registry, store).claim_next(
        "binding-conflict-worker", ("repository",), reconcile_first=False
    )["run"]
    store.bind(run["run_id"], "executor-a", "external-a")
    run_before = store.run(run["run_id"])
    events_before = run_events(store, run["run_id"])
    registry_before = registry_file_evidence(registry_root)

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: canonical_runtime_identity(registry_root),
    )
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())

    conflicts = [
        ("executor-b", "external-a", "run already targets another external system"),
        ("executor-a", "external-b", "run already has another external binding"),
    ]
    for system, external_id, expected_detail in conflicts:
        exit_code = bureau_cli.main(
            [
                "--state-root",
                str(state_root),
                "--json",
                "bind",
                run["run_id"],
                "--system",
                system,
                "--external-id",
                external_id,
            ]
        )

        assert exit_code == 2
        value = json.loads(capsys.readouterr().out)
        assert value["result"]["status"] == "failed"
        assert value["result"]["detail"] == expected_detail
        assert value["runtime_identity"]["command_effect_scope"] == (
            "coordination_state_mutation"
        )
        assert store.run(run["run_id"]) == run_before
        assert run_events(store, run["run_id"]) == events_before

    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_bind_runtime_identity_drift_stops_before_binding(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    state_root = tmp_path / "coordination-state"
    registry = Registry.load(registry_root)
    store = StateStore(state_root / "bureau.sqlite3")
    run = Dispatcher(registry, store).claim_next(
        "runtime-drift-worker", ("repository",), reconcile_first=False
    )["run"]
    run_before = store.run(run["run_id"])
    events_before = run_events(store, run["run_id"])
    registry_before = registry_file_evidence(registry_root)
    first = canonical_runtime_identity(registry_root)
    second = canonical_runtime_identity(registry_root)
    second["manifest"]["valid"] = False
    identities = iter([first, second])

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(
        bureau_cli, "bureau_runtime_identity", lambda *args, **kwargs: next(identities)
    )
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())

    exit_code = bureau_cli.main(
        [
            "--state-root",
            str(state_root),
            "--json",
            "bind",
            run["run_id"],
            "--system",
            "codex",
            "--external-id",
            "session-drift",
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "coordination-runtime-identity-blocked"
    assert "runtime-manifest-invalid" in value["result"]["reason_codes"]
    assert store.run(run["run_id"]) == run_before
    assert run_events(store, run["run_id"]) == events_before
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_bind_state_binding_drift_stops_before_store_open(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    state_root = tmp_path / "coordination-state"
    state_db = state_root / "bureau.sqlite3"
    registry_before = registry_file_evidence(registry_root)
    identity_call_count = 0

    def drifting_runtime_identity(*args, **kwargs) -> dict:
        nonlocal identity_call_count
        identity_call_count += 1
        if identity_call_count == 2:
            state_root.mkdir()
            state_db.touch()
        return canonical_runtime_identity(registry_root)

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(
        bureau_cli, "bureau_runtime_identity", drifting_runtime_identity
    )
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())

    exit_code = bureau_cli.main(
        [
            "--state-root",
            str(state_root),
            "--json",
            "bind",
            "BUR-RUN-TEST",
            "--system",
            "codex",
            "--external-id",
            "session-state-drift",
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "coordination-state-binding-changed"
    assert value["result"]["reason_codes"] == [
        "coordination-state-binding-changed-before-open"
    ]
    assert identity_call_count == 2
    assert state_db.read_bytes() == b""
    assert sorted(path.name for path in state_root.iterdir()) == ["bureau.sqlite3"]
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_complete_is_coordination_gated_but_rejects_active_run(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    state_root = tmp_path / "coordination-state"
    state_db = state_root / "bureau.sqlite3"
    registry = Registry.load(registry_root)
    store = StateStore(state_db)
    run = Dispatcher(registry, store).claim_next(
        "complete-worker", ("repository",), reconcile_first=False
    )["run"]
    run_before = store.run(run["run_id"])
    events_before = run_events(store, run["run_id"])
    registry_before = registry_file_evidence(registry_root)
    evidence_path = tmp_path / "completion.json"
    evidence_path.write_text(
        json.dumps({"proof": {"result": "passed"}}), encoding="utf-8"
    )
    source_commit = registry_before["tree_sha256"][:40]
    identity_calls: list[tuple[Path, Path]] = []

    def runtime_identity(root: Path, *, state_path: Path) -> dict:
        identity_calls.append((root.resolve(), state_path))
        return canonical_runtime_identity(
            registry_root,
            commit=source_commit,
            tree_sha256=registry_before["tree_sha256"],
        )

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", runtime_identity)
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())

    exit_code = bureau_cli.main(
        [
            "--state-root",
            str(state_root),
            "--json",
            "complete",
            run["run_id"],
            "--evidence",
            str(evidence_path),
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    error = value["result"]
    assert error["code"] == "typed-acceptance-required"
    assert error["effect_applied"] is False
    runtime = value["runtime_identity"]
    assert runtime["command_effect_scope"] == "coordination_state_mutation"
    binding = runtime["coordination_state_binding"]
    assert binding["registry_source_commit"] == source_commit
    assert binding["registry_tree_sha256"] == registry_before["tree_sha256"]
    assert binding["state_root"] == str(state_root)
    assert binding["state_db"] == str(state_db)
    assert identity_calls == [
        (registry_root.resolve(), state_db),
        (registry_root.resolve(), state_db),
    ]

    run_after = store.run(run["run_id"])
    assert run_before["state"] == "assigned"
    assert run_before["reservations"]
    assert run_after == run_before
    assert store.receipt(run["run_id"]) is None
    assert run_events(store, run["run_id"]) == events_before
    assert registry_file_evidence(registry_root) == registry_before


def test_canonical_complete_rejects_direct_close_before_stale_baseline_check(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    state_root = tmp_path / "coordination-state"
    registry = Registry.load(registry_root)
    store = StateStore(state_root / "bureau.sqlite3")
    run = Dispatcher(registry, store).claim_next(
        "complete-drift-worker", ("repository",), reconcile_first=False
    )["run"]

    task_path = next((registry_root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["title"] = task["title"] + " drift"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    registry_before = registry_file_evidence(registry_root)
    run_before = store.run(run["run_id"])
    events_before = run_events(store, run["run_id"])
    evidence_path = tmp_path / "completion-drift.json"
    evidence_path.write_text(
        json.dumps({"proof": {"result": "passed"}}), encoding="utf-8"
    )

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: canonical_runtime_identity(
            registry_root, tree_sha256=registry_before["tree_sha256"]
        ),
    )
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())

    exit_code = bureau_cli.main(
        [
            "--state-root",
            str(state_root),
            "--json",
            "complete",
            run["run_id"],
            "--evidence",
            str(evidence_path),
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    error = value.get("result", value)
    assert error["code"] == "typed-acceptance-required"
    assert error["effect_applied"] is False
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )
    assert store.run(run["run_id"]) == run_before
    assert run_events(store, run["run_id"]) == events_before
    assert store.receipt(run["run_id"]) is None
    assert registry_file_evidence(registry_root) == registry_before

def test_canonical_bind_rejects_state_path_inside_registry_before_effect(
    registry_factory, monkeypatch, capsys
) -> None:
    registry_root = registry_factory(1)
    unsafe_state_root = registry_root / "coordination-state"
    registry_before = registry_file_evidence(registry_root)

    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(registry_root))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: canonical_runtime_identity(registry_root),
    )
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())

    exit_code = bureau_cli.main(
        [
            "--state-root",
            str(unsafe_state_root),
            "--json",
            "bind",
            "BUR-RUN-TEST",
            "--system",
            "codex",
            "--external-id",
            "session-invalid-path",
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "coordination-state-path-invalid"
    assert "coordination-state-root-overlaps-registry" in value["result"]["reason_codes"]
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )
    assert not unsafe_state_root.exists()
    assert registry_file_evidence(registry_root) == registry_before
