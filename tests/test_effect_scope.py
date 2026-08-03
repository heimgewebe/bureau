from __future__ import annotations

import json
from pathlib import Path

from bureau import cli as bureau_cli
from bureau import effect_scope


def canonical_runtime_identity(root: Path, *, commit: str = "a" * 40) -> dict:
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


def test_command_effect_scope_is_explicit_and_fails_closed() -> None:
    assert effect_scope.classify_command_effect_scope("status", mutates=False) == "read_only"
    assert (
        effect_scope.classify_command_effect_scope("claim-commit", mutates=True)
        == "coordination_state_mutation"
    )
    assert (
        effect_scope.classify_command_effect_scope("future-command", mutates=True)
        == "registry_mutation"
    )


def test_claim_intent_is_coordination_state_mutation() -> None:
    assert bureau_cli._command_mutates(
        bureau_cli.parser().parse_args(["claim-intent", "--worker", "test-worker"])
    )
    assert (
        effect_scope.classify_command_effect_scope("claim-intent", mutates=True)
        == "coordination_state_mutation"
    )


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
