from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_approval import write_runtime_approval_intent

from bureau import cli as bureau_cli
from bureau.cycle_deployment import STAGES
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
    shutil.copytree(project_root / "src/bureau_cycle", source / "src/bureau_cycle")
    shutil.copytree(project_root / "ops/systemd", source / "ops/systemd")
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
    approval = write_runtime_approval_intent(source, tmp_path, label="canonical")
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
            "--approval-intent",
            str(approval),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
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
    assert bureau_cli._command_mutates(SimpleNamespace(command="verification-stamp")) is False
    assert bureau_cli._command_mutates(SimpleNamespace(command="doctor", repair=False)) is False
    assert bureau_cli._command_mutates(SimpleNamespace(command="doctor", repair=True)) is True
    assert (
        bureau_cli._command_mutates(
            SimpleNamespace(command="queue-reconcile", write_plan=None, apply_plan=None)
        )
        is False
    )


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
    status_capsule_launcher = Path(receipt["status_capsule_launcher_path"])

    assert receipt["canonical_registry_root"] == str(snapshot)
    assert manifest["status_capsule_launcher_path"] == str(status_capsule_launcher)
    assert status_capsule_launcher.is_file()
    assert snapshot.is_dir()
    assert inventory.is_file()
    assert stat.S_IMODE((snapshot / "registry/queue.json").stat().st_mode) == 0o444

    status_read = subprocess.run(
        [
            str(status_capsule_launcher),
            "read",
            "--path",
            str(tmp_path / "missing-status-capsule.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert status_read.returncode == 2
    assert json.loads(status_read.stdout)["status"] == "unavailable"

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

    verification_state_root = tmp_path / "verification-state"
    expected_verification = json.loads(
        (source / "registry/tasks/GRABOWSKI-OPERATOR-SURFACE-V1-T154.json").read_text(
            encoding="utf-8"
        )
    )["metadata"]["verification"]
    verification_run = subprocess.run(
        [
            str(launcher),
            "--state-root",
            str(verification_state_root),
            "--json",
            "verification-stamp",
            "GRABOWSKI-OPERATOR-SURFACE-V1-T154",
        ],
        cwd=unrelated,
        check=True,
        capture_output=True,
        text=True,
    )
    verification = json.loads(verification_run.stdout)
    assert verification["result"]["receipt_sha256"] == expected_verification["receipt_sha256"]
    assert verification["result"]["task_sha256"] == expected_verification["task_sha256"]
    assert verification["result"]["plan_sha256"] == expected_verification["plan_sha256"]
    assert verification["runtime_identity"]["command_effect_scope"] == "read_only"
    assert verification["runtime_identity"]["state"]["available"] is False
    assert not verification_state_root.exists()

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

    intent = tmp_path / "claim-intent.json"
    intent.write_text("{}\n", encoding="utf-8")
    missing_state_root = subprocess.run(
        [
            str(launcher),
            "--json",
            "claim-commit",
            "--intent",
            str(intent),
        ],
        cwd=unrelated,
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_state_root.returncode == 2
    missing_state = json.loads(missing_state_root.stdout)
    assert missing_state["result"]["status"] == "explicit-coordination-state-root-required"
    assert not (unrelated / "bureau.sqlite3").exists()

    overlapping_state_root = subprocess.run(
        [
            str(launcher),
            "--state-root",
            str(snapshot / "state"),
            "--json",
            "claim-commit",
            "--intent",
            str(intent),
        ],
        cwd=unrelated,
        check=False,
        capture_output=True,
        text=True,
    )
    assert overlapping_state_root.returncode == 2
    overlap = json.loads(overlapping_state_root.stdout)
    assert overlap["result"]["status"] == "coordination-state-path-invalid"
    assert "coordination-state-root-overlaps-registry" in overlap["result"]["reason_codes"]

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


def test_runtime_release_excludes_unmanaged_package_artifacts(tmp_path: Path) -> None:
    source = make_installable_source(tmp_path)
    exclude = source / ".git/info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("__pycache__/\n*.pyc\noperator-note.txt\n", encoding="utf-8")
    cache = source / "src/bureau/__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "operator-generated.pyc").write_bytes(b"not-bytecode")
    (source / "src/bureau/operator-note.txt").write_text(
        "untracked operator residue\n", encoding="utf-8"
    )
    assert git(source, "status", "--short") == ""

    _launcher, prefix, _receipt = install_runtime(tmp_path, source)
    manifest = json.loads((prefix / "deployment-manifest.json").read_text(encoding="utf-8"))
    release = Path(manifest["immutable_release_path"])
    package = release / "src/bureau"

    assert not (package / "__pycache__").exists()
    assert not (package / "operator-note.txt").exists()
    files = [path for path in package.rglob("*") if path.is_file()]
    assert files
    assert all(path.suffix == ".py" for path in files)


def test_runtime_release_contains_cycle_scheduler_package(tmp_path: Path) -> None:
    source = make_installable_source(tmp_path)
    _launcher, prefix, _receipt = install_runtime(tmp_path, source)
    manifest = json.loads((prefix / "deployment-manifest.json").read_text(encoding="utf-8"))
    release = Path(manifest["immutable_release_path"])
    package = release / "src/bureau_cycle"

    assert package.is_dir()
    assert (package / "__init__.py").is_file()
    assert (package / "discovery_runner.py").is_file()
    assert (package / "verifier_runner.py").is_file()


def test_deployed_launcher_runs_cycle_deployment_audit_from_immutable_release(
    tmp_path: Path,
) -> None:
    source = make_installable_source(tmp_path)
    launcher, prefix, _receipt = install_runtime(tmp_path, source)
    manifest = json.loads((prefix / "deployment-manifest.json").read_text(encoding="utf-8"))
    release = Path(manifest["immutable_release_path"])
    units = tmp_path / "units"
    shims = tmp_path / "shims"
    units.mkdir()
    shims.mkdir()
    for _stage, name, _module in STAGES:
        shutil.copy2(release / "ops/systemd" / f"{name}.service", units / f"{name}.service")
        shutil.copy2(release / "ops/systemd" / f"{name}.timer", units / f"{name}.timer")
        shutil.copy2(release / "ops/systemd/libexec" / name, shims / name)

    completed = subprocess.run(
        [
            str(launcher),
            "--json",
            "cycle-deployment",
            "--unit-root",
            str(units),
            "--shim-root",
            str(shims),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    result = payload["result"]

    assert result["status"] == "ok"
    assert result["read_only"] is True
    assert result["self_heal"] is False
    assert result["release_identity"]["matches"] is True
    assert payload["runtime_identity"]["module"]["source_kind"] == "immutable-release"


def test_installer_rejects_existing_release_with_unmanaged_entry(tmp_path: Path) -> None:
    source = make_installable_source(tmp_path)
    _launcher, prefix, _receipt = install_runtime(tmp_path, source)
    manifest_path = prefix / "deployment-manifest.json"
    manifest_before = manifest_path.read_bytes()
    manifest = json.loads(manifest_before)
    package = Path(manifest["immutable_release_path"]) / "src/bureau"
    package.chmod(0o755)
    unmanaged = package / "runtime-residue.pyc"
    unmanaged.write_bytes(b"unexpected")

    project_root = Path(__file__).resolve().parents[1]
    approval = write_runtime_approval_intent(source, tmp_path, label="unmanaged")
    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "ops/install-bureau-runtime.py"),
            "--source",
            str(source),
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(tmp_path / "bin"),
            "--approval-intent",
            str(approval),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "immutable release contains unmanaged entry" in completed.stderr
    assert manifest_path.read_bytes() == manifest_before


def test_missing_state_uses_ephemeral_read_only_schema(tmp_path: Path) -> None:
    state_root = tmp_path / "missing-state"
    state = state_root / "bureau.sqlite3"
    store = ReadOnlyStateStore(state, state_root)

    assert store.list_runs() == []
    assert not state_root.exists()
    with store.connect() as read, pytest.raises(sqlite3.OperationalError):
        read.execute("CREATE TABLE forbidden(value TEXT)")
    assert not state_root.exists()
