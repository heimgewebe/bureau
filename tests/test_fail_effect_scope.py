from __future__ import annotations

import json
from pathlib import Path

from bureau import cli as bureau_cli


def _canonical_runtime_identity(root: Path, *, commit: str = "a" * 40) -> dict:
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


def test_canonical_fail_uses_explicit_coordination_state_store(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    state_root = tmp_path / "coordination-state"
    observed: dict[str, str] = {}

    def fake_fail_run(store, run_id: str, error: str):
        observed.update(state_db=str(store.path), run_id=run_id, error=error)
        return {"run_id": run_id, "state": "failed", "error": error}

    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: _canonical_runtime_identity(registry_root),
    )
    monkeypatch.setattr(bureau_cli, "adapters", lambda args: object())
    monkeypatch.setattr(bureau_cli, "fail_run", fake_fail_run)

    exit_code = bureau_cli.main(
        [
            "--root",
            str(registry_root),
            "--state-root",
            str(state_root),
            "--json",
            "fail",
            "BUR-RUN-TEST",
            "--error",
            "orphan-reconcile",
        ]
    )

    assert exit_code == 0
    value = json.loads(capsys.readouterr().out)
    assert observed == {
        "state_db": str(state_root / "bureau.sqlite3"),
        "run_id": "BUR-RUN-TEST",
        "error": "orphan-reconcile",
    }
    assert value["result"]["state"] == "failed"
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )
    assert value["runtime_identity"]["coordination_state_binding"]["state_root"] == str(
        state_root
    )
    assert (state_root / "bureau.sqlite3").is_file()


def test_canonical_fail_without_explicit_state_root_stops_before_fail_run(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry_root = registry_factory()
    implicit_state_root = tmp_path / "implicit-state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(implicit_state_root))
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: _canonical_runtime_identity(registry_root),
    )
    monkeypatch.setattr(
        bureau_cli,
        "fail_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fail_run must not execute")
        ),
    )

    exit_code = bureau_cli.main(
        [
            "--root",
            str(registry_root),
            "--json",
            "fail",
            "BUR-RUN-TEST",
            "--error",
            "orphan-reconcile",
        ]
    )

    assert exit_code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["result"]["status"] == "explicit-coordination-state-root-required"
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )
    assert not implicit_state_root.exists()
