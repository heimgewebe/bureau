from __future__ import annotations

import json
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bureau import cli as bureau_cli
from bureau.read_only_state import ReadOnlyStateStore


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def make_installable_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    shutil.copytree(project_root / "src/bureau", source / "src/bureau")
    shutil.copytree(project_root / "registry", source / "registry")
    shutil.copytree(project_root / "schemas", source / "schemas")
    shutil.copy2(project_root / "pyproject.toml", source / "pyproject.toml")
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    git(source, "add", ".")
    git(source, "commit", "-m", "source")
    git(source, "remote", "add", "origin", str(source / ".git"))
    git(source, "fetch", "origin", "main:refs/remotes/origin/main")
    return source


def install_runtime(tmp_path: Path, source: Path) -> tuple[Path, Path, dict]:
    project_root = Path(__file__).resolve().parents[1]
    prefix = tmp_path / "runtime"
    bin_dir = tmp_path / "bin"
    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "ops/install-bureau-runtime.py"),
            "--source",
            str(source),
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return bin_dir / "bureau", prefix, json.loads(completed.stdout)


def test_registry_root_resolution_precedence(tmp_path: Path, monkeypatch) -> None:
    configured = tmp_path / "configured"
    environment = tmp_path / "environment"
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT", str(environment))
    monkeypatch.setenv("BUREAU_REGISTRY_ROOT_MODE", "canonical-runtime-default")

    root, mode = bureau_cli.resolve_registry_root(str(configured))
    assert root == configured
    assert mode == "explicit-cli"

    root, mode = bureau_cli.resolve_registry_root(None)
    assert root == environment
    assert mode == "canonical-runtime-default"

    monkeypatch.delenv("BUREAU_REGISTRY_ROOT")
    monkeypatch.delenv("BUREAU_REGISTRY_ROOT_MODE")
    monkeypatch.chdir(tmp_path)
    root, mode = bureau_cli.resolve_registry_root(None)
    assert root == tmp_path
    assert mode == "ambient-cwd"


def test_statement_and_mutation_classification() -> None:
    assert bureau_cli._command_mutates(SimpleNamespace(command="status")) is False
    assert bureau_cli._command_mutates(SimpleNamespace(command="what-now")) is False
    assert bureau_cli._command_mutates(
        SimpleNamespace(command="doctor", repair=False)
    ) is False
    assert bureau_cli._command_mutates(
        SimpleNamespace(command="doctor", repair=True)
    ) is True
    assert bureau_cli._command_mutates(
        SimpleNamespace(command="queue-reconcile", write_plan=None, apply_plan=None)
    ) is False


def test_read_only_state_store_has_no_initialization_side_effect(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    state = state_root / "bureau.sqlite3"
    connection = sqlite3.connect(state)
    connection.execute("CREATE TABLE marker(value TEXT)")
    connection.execute("INSERT INTO marker VALUES ('ok')")
    connection.commit()
    connection.close()

    store = ReadOnlyStateStore(state, state_root)
    assert not (state_root / "envelopes").exists()
    assert not (state_root / "receipts").exists()
    with store.connect() as read:
        assert read.execute("SELECT value FROM marker").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.OperationalError):
            read.execute("INSERT INTO marker VALUES ('forbidden')")


def test_deployed_launcher_uses_hash_bound_canonical_registry(tmp_path: Path) -> None:
    source = make_installable_source(tmp_path)
    launcher, prefix, receipt = install_runtime(tmp_path, source)
    manifest = json.loads((prefix / "deployment-manifest.json").read_text(encoding="utf-8"))
    snapshot = Path(manifest["canonical_registry_root"])
    inventory = Path(manifest["canonical_registry_inventory_path"])

    assert receipt["canonical_registry_root"] == str(snapshot)
    assert snapshot.is_dir()
    assert inventory.is_file()
    assert stat.S_IMODE((snapshot / "registry/queue.json").stat().st_mode) == 0o444

    unrelated = tmp_path / "unrelated-dirty-checkout"
    unrelated.mkdir()
    (unrelated / "foreign-change.txt").write_text("do not touch\n", encoding="utf-8")

    identity_run = subprocess.run(
        [str(launcher), "--json", "runtime-identity"],
        cwd=unrelated,
        check=True,
        capture_output=True,
        text=True,
    )
    identity = json.loads(identity_run.stdout)["runtime_identity"]
    assert identity["registry_selection"] == "canonical-runtime-default"
    assert identity["registry"]["role"] == "canonical-runtime-snapshot"
    assert identity["registry"]["root"] == str(snapshot)
    assert identity["compatibility"]["status"] == "canonical-read-only"
    assert identity["compatibility"]["mutation_allowed"] is False

    check_run = subprocess.run(
        [str(launcher), "--json", "check"],
        cwd=unrelated,
        check=True,
        capture_output=True,
        text=True,
    )
    check = json.loads(check_run.stdout)
    assert check["result"]["valid"] is True
    assert check["runtime_identity"]["registry"]["root"] == str(snapshot)

    blocked_write = subprocess.run(
        [str(launcher), "--json", "close-ready"],
        cwd=unrelated,
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked_write.returncode == 2
    blocked = json.loads(blocked_write.stdout)
    assert blocked["result"]["status"] == "explicit-registry-root-required"

    explicit = subprocess.run(
        [str(launcher), "--root", str(source), "--json", "runtime-identity"],
        cwd=unrelated,
        check=True,
        capture_output=True,
        text=True,
    )
    explicit_identity = json.loads(explicit.stdout)["runtime_identity"]
    assert explicit_identity["registry_selection"] == "explicit-cli"
    assert explicit_identity["compatibility"]["status"] == "compatible"
    assert explicit_identity["compatibility"]["mutation_allowed"] is True

    queue = snapshot / "registry/queue.json"
    queue.chmod(0o644)
    queue.write_text(queue.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    tampered = subprocess.run(
        [str(launcher), "--json", "check"],
        cwd=unrelated,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tampered.returncode == 2
    tampered_result = json.loads(tampered.stdout)
    assert tampered_result["result"]["status"] == "canonical-registry-invalid"
    assert "tree-digest-mismatch" in tampered_result["result"]["reason_codes"]


def test_missing_state_uses_ephemeral_read_only_schema(tmp_path: Path) -> None:
    state_root = tmp_path / "missing-state"
    state = state_root / "bureau.sqlite3"
    store = ReadOnlyStateStore(state, state_root)

    assert store.list_runs() == []
    assert not state_root.exists()
    with store.connect() as read, pytest.raises(sqlite3.OperationalError):
        read.execute("CREATE TABLE forbidden(value TEXT)")
    assert not state_root.exists()
