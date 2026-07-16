from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from bureau import cli as bureau_cli
from bureau.runtime_identity import (
    _package_tree_sha256,
    bureau_runtime_identity,
    require_mutation_compatible,
)


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def init_repo(root: Path) -> None:
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "src/bureau").mkdir(parents=True)
    (root / "src/bureau/runtime_identity.py").write_text("# test\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    git(root, "remote", "add", "origin", str(root / ".git"))
    git(root, "fetch", "origin", "main:refs/remotes/origin/main")


def test_same_checkout_runtime_is_compatible(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root)
    module = root / "src/bureau/runtime_identity.py"
    identity = bureau_runtime_identity(root, module_path=module)
    assert identity["compatibility"]["status"] == "compatible"
    assert identity["compatibility"]["mutation_allowed"] is True
    assert identity["module"]["source_kind"] == "same-checkout"


def test_dirty_same_checkout_blocks_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root)
    module = root / "src/bureau/runtime_identity.py"
    (root / "dirty.txt").write_text("dirty", encoding="utf-8")
    identity = bureau_runtime_identity(root, module_path=module)
    assert identity["compatibility"]["status"] == "dirty"
    assert identity["compatibility"]["mutation_allowed"] is False
    assert identity["compatibility"]["reason_codes"] == ["source-checkout-dirty"]


def test_unbound_runtime_blocks_mutation(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    init_repo(root)
    module = tmp_path / "site-packages/bureau/runtime_identity.py"
    module.parent.mkdir(parents=True)
    module.write_text("# ambient\n", encoding="utf-8")
    monkeypatch.setenv("BUREAU_RUNTIME_MANIFEST", str(tmp_path / "missing.json"))
    identity = bureau_runtime_identity(root, module_path=module)
    assert identity["compatibility"]["status"] == "unbound"
    assert require_mutation_compatible(identity)["status"] == "stale-runtime-blocked"


def test_manifest_bound_release_matches_registry(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    init_repo(root)
    head = git(root, "rev-parse", "HEAD")
    release = tmp_path / "release"
    module = release / "src/bureau/runtime_identity.py"
    module.parent.mkdir(parents=True)
    module.write_text("# release\n", encoding="utf-8")
    (release / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    tree_sha256 = _package_tree_sha256(release)
    assert tree_sha256 is not None
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bureau_runtime_deployment",
                "release_id": "test",
                "immutable_release_path": str(release),
                "module_path": str(module),
                "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                "package_tree_sha256": tree_sha256,
                "source_commit": head,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BUREAU_RUNTIME_MANIFEST", str(manifest))
    identity = bureau_runtime_identity(root, module_path=module)
    assert identity["compatibility"]["status"] == "compatible"
    assert identity["module"]["source_kind"] == "immutable-release"
    assert identity["manifest"]["observed_package_tree_sha256"] == tree_sha256


def test_state_identity_is_visible(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root)
    state = tmp_path / "bureau.sqlite3"
    connection = sqlite3.connect(state)
    connection.execute("PRAGMA user_version = 7")
    connection.close()
    identity = bureau_runtime_identity(
        root,
        state_path=state,
        module_path=root / "src/bureau/runtime_identity.py",
    )
    assert identity["state"]["schema_version"] == 7
    assert identity["state"]["integrity"] == "ok"


def test_json_dict_gets_runtime_identity(registry_factory, capsys) -> None:
    root = registry_factory()
    result = bureau_cli.main(["--root", str(root), "--json", "check"])
    assert result == 0
    value = json.loads(capsys.readouterr().out)
    assert value["valid"] is True
    assert value["runtime_identity"]["kind"] == "bureau_runtime_identity"


def test_operational_registry_uses_json_envelope(capsys) -> None:
    root = Path(__file__).resolve().parents[1]
    result = bureau_cli.main(["--root", str(root), "--json", "runtime-identity"])
    assert result == 0
    value = json.loads(capsys.readouterr().out)
    assert value["runtime_identity"]["kind"] == "bureau_runtime_identity"
    assert value["result"] == {"status": "ok"}


def test_json_envelope_preserves_list_result(monkeypatch, capsys) -> None:
    identity = {"kind": "bureau_runtime_identity"}
    monkeypatch.setattr(bureau_cli, "_CLI_RUNTIME_IDENTITY", identity)
    monkeypatch.setattr(bureau_cli, "_CLI_JSON_ENVELOPE", True)
    bureau_cli.emit([{"id": 1}], True)
    value = json.loads(capsys.readouterr().out)
    assert value["runtime_identity"] == identity
    assert value["result"] == [{"id": 1}]


def test_command_classification_fails_closed_for_unknown_commands() -> None:
    assert bureau_cli._command_mutates(SimpleNamespace(command="future-command")) is True
    assert bureau_cli._command_mutates(SimpleNamespace(command="source-check")) is False
    assert bureau_cli._command_mutates(
        SimpleNamespace(command="source-sync", apply=False)
    ) is False
    assert bureau_cli._command_mutates(
        SimpleNamespace(command="source-sync", apply=True)
    ) is True
    assert bureau_cli._command_mutates(
        SimpleNamespace(command="worktree-hygiene", write_plan=None, apply_plan=None)
    ) is False
    assert bureau_cli._command_mutates(SimpleNamespace(command="doctor")) is True
    assert (
        bureau_cli._command_mutates(SimpleNamespace(command="migrate-leases")) is True
    )
    assert (
        bureau_cli._command_mutates(
            SimpleNamespace(
                command="migrate-leases",
                apply_plan=None,
                write_plan=None,
            )
        )
        is False
    )


def test_mutation_gate_blocks_incompatible_runtime(registry_factory, monkeypatch, capsys) -> None:
    root = registry_factory()
    blocked_identity = {
        "registry": {"bureau_project": False},
        "compatibility": {
            "status": "unbound",
            "mutation_allowed": False,
            "reason_codes": ["runtime-not-bound-to-registry"],
        },
    }
    monkeypatch.setattr(bureau_cli, "bureau_runtime_identity", lambda *a, **k: blocked_identity)
    result = bureau_cli.main(["--root", str(root), "--json", "close-ready"])
    assert result == 2
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "stale-runtime-blocked"
    assert value["runtime_identity"] == blocked_identity


def test_immutable_installer_launcher_and_rollback(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    shutil.copytree(project_root / "src/bureau", source / "src/bureau")
    shutil.copy2(project_root / "pyproject.toml", source / "pyproject.toml")
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    git(source, "add", ".")
    git(source, "commit", "-m", "source")
    git(source, "remote", "add", "origin", str(source / ".git"))
    git(source, "fetch", "origin", "main:refs/remotes/origin/main")
    prefix = tmp_path / "runtime"
    bin_dir = tmp_path / "bin"
    command = [
        sys.executable,
        str(project_root / "ops/install-bureau-runtime.py"),
        "--source",
        str(source),
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bin_dir),
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    first_receipt = json.loads(first.stdout)
    assert Path(first_receipt["receipt_path"]).is_file()
    second = subprocess.run(command, check=True, capture_output=True, text=True)
    second_receipt = json.loads(second.stdout)
    rollback = second_receipt["rollback"]
    assert Path(rollback["manifest"]).is_file()
    assert Path(rollback["launcher"]).is_file()

    launcher = bin_dir / "bureau"
    launched = subprocess.run(
        [str(launcher), "--root", str(source), "--json", "runtime-identity"],
        check=True,
        capture_output=True,
        text=True,
    )
    envelope = json.loads(launched.stdout)
    identity = envelope["runtime_identity"]
    assert identity["compatibility"]["status"] == "compatible"
    assert identity["module"]["source_kind"] == "immutable-release"
    assert envelope["result"] == {"status": "ok"}

    manifest = prefix / "deployment-manifest.json"
    original_manifest = manifest.read_bytes()
    manifest.write_bytes(original_manifest + b" ")
    drifted_manifest = subprocess.run(
        [str(launcher), "--root", str(source), "--json", "runtime-identity"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert drifted_manifest.returncode != 0
    assert "manifest digest mismatch" in drifted_manifest.stderr
    manifest.write_bytes(original_manifest)

    deployment = json.loads(original_manifest)
    release_cli = Path(deployment["immutable_release_path"]) / "src/bureau/cli.py"
    release_cli.chmod(0o644)
    release_cli.write_text(release_cli.read_text(encoding="utf-8") + "\n# drift\n")
    drifted_package = subprocess.run(
        [str(launcher), "--root", str(source), "--json", "runtime-identity"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert drifted_package.returncode != 0
    assert "package tree digest mismatch" in drifted_package.stderr



def test_installer_migrates_existing_launcher_symlink_only_with_explicit_replace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    shutil.copytree(project_root / "src/bureau", source / "src/bureau")
    shutil.copy2(project_root / "pyproject.toml", source / "pyproject.toml")
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    git(source, "add", ".")
    git(source, "commit", "-m", "source")
    git(source, "remote", "add", "origin", str(source / ".git"))
    git(source, "fetch", "origin", "main:refs/remotes/origin/main")

    home = tmp_path / "home"
    prefix = home / ".local/share/bureau"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    legacy_target = prefix / "venv/bin/bureau"
    legacy_target.parent.mkdir(parents=True)
    legacy_target.write_text("legacy bureau launcher\n", encoding="utf-8")
    legacy_target.chmod(0o755)
    launcher = bin_dir / "bureau"
    raw_target = "../share/bureau/venv/bin/bureau"
    launcher.symlink_to(raw_target)
    legacy_sha256 = hashlib.sha256(legacy_target.read_bytes()).hexdigest()

    command = [
        sys.executable,
        str(project_root / "ops/install-bureau-runtime.py"),
        "--source",
        str(source),
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bin_dir),
    ]
    blocked = subprocess.run(command, check=False, capture_output=True, text=True)
    assert blocked.returncode != 0
    assert "launcher is a symlink" in blocked.stderr
    assert launcher.is_symlink()
    assert launcher.readlink().as_posix() == raw_target

    migrated = subprocess.run(
        [*command, "--replace-existing"],
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(migrated.stdout)
    assert launcher.is_file()
    assert not launcher.is_symlink()
    assert hashlib.sha256(legacy_target.read_bytes()).hexdigest() == legacy_sha256
    rollback = receipt["rollback"]
    assert rollback["launcher_kind"] == "symlink"
    assert rollback["launcher_symlink_target"] == raw_target
    metadata_path = Path(rollback["launcher_metadata"])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata == {
        "schema_version": 1,
        "kind": "bureau_launcher_symlink_backup",
        "path": str(launcher),
        "target": raw_target,
    }
