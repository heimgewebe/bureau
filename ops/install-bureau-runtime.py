#!/usr/bin/env python3
"""Install Bureau as an immutable, manifest-bound local runtime."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANAGED_MARKER = "# managed-by: heimgewebe-bureau-runtime-v1"


def sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"expected regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> bytes:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{rendered}\n".encode()


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def git(source: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )
    if completed.returncode != 0:
        raise SystemExit(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.rstrip("\n")


def package_tree_sha256(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    package = root / "src/bureau"
    if pyproject.is_symlink() or not pyproject.is_file() or not package.is_dir():
        raise SystemExit(f"invalid Bureau package tree: {root}")
    paths = [pyproject, *sorted(package.rglob("*.py"))]
    digest = hashlib.sha256()
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"package tree contains non-regular input: {path}")
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def tracked_paths(source: Path) -> list[Path]:
    raw = git(source, "ls-files", "-z")
    paths: list[Path] = []
    for item in raw.split("\0"):
        if not item:
            continue
        relative = Path(item)
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"unsafe tracked path: {item}")
        paths.append(relative)
    if not paths:
        raise SystemExit("Bureau source has no tracked files")
    return sorted(paths, key=lambda path: path.as_posix())


def tracked_tree_sha256(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"tracked tree contains non-regular input: {path}")
        encoded = relative.as_posix().encode()
        content = path.read_bytes()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def ensure_registry_snapshot(
    source: Path,
    prefix: Path,
    head: str,
) -> dict[str, str]:
    paths = tracked_paths(source)
    tree_digest = tracked_tree_sha256(source, paths)
    snapshot_id = f"{head[:12]}-tree{tree_digest[:12]}"
    snapshot = prefix / "registry-snapshots" / snapshot_id
    inventory = snapshot / ".bureau-runtime-snapshot.json"
    inventory_value = {
        "schema_version": 1,
        "kind": "bureau_registry_snapshot",
        "source_commit": head,
        "tree_sha256": tree_digest,
        "paths": [path.as_posix() for path in paths],
    }
    inventory_bytes = canonical(inventory_value)
    inventory_digest = hashlib.sha256(inventory_bytes).hexdigest()

    if not snapshot.exists():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        temporary = snapshot.parent / f".{snapshot_id}.tmp-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir()
        try:
            for relative in paths:
                source_path = source / relative
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination, follow_symlinks=False)
            atomic_write(
                temporary / inventory.name,
                inventory_bytes,
                0o444,
            )
            if tracked_tree_sha256(temporary, paths) != tree_digest:
                raise SystemExit("copied Bureau Registry snapshot digest mismatch")
            for path in sorted(temporary.rglob("*"), reverse=True):
                if path.is_file():
                    executable = bool(path.stat().st_mode & stat.S_IXUSR)
                    path.chmod(0o555 if executable else 0o444)
                elif path.is_dir():
                    path.chmod(0o555)
            temporary.chmod(0o555)
            os.replace(temporary, snapshot)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    if snapshot.is_symlink() or not snapshot.is_dir():
        raise SystemExit("existing Bureau Registry snapshot is not a directory")
    if inventory.is_symlink() or not inventory.is_file():
        raise SystemExit("existing Bureau Registry snapshot inventory is invalid")
    if hashlib.sha256(inventory.read_bytes()).hexdigest() != inventory_digest:
        raise SystemExit("existing Bureau Registry snapshot inventory digest mismatch")
    if tracked_tree_sha256(snapshot, paths) != tree_digest:
        raise SystemExit("existing Bureau Registry snapshot tree digest mismatch")
    return {
        "root": str(snapshot),
        "inventory_path": str(inventory),
        "inventory_sha256": inventory_digest,
        "tree_sha256": tree_digest,
    }


def wrapper(manifest_path: Path, manifest_sha256: str) -> bytes:
    return f'''#!/usr/bin/env python3
{MANAGED_MARKER}
import hashlib
import json
import os
import sys
from pathlib import Path

manifest_path = Path({str(manifest_path)!r})
expected_manifest_sha256 = {manifest_sha256!r}
if manifest_path.is_symlink() or not manifest_path.is_file():
    raise SystemExit("bureau runtime manifest is not a regular file")
manifest_bytes = manifest_path.read_bytes()
if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
    raise SystemExit("bureau runtime manifest digest mismatch")
try:
    manifest = json.loads(manifest_bytes)
    if manifest["schema_version"] != 1 or manifest["kind"] != "bureau_runtime_deployment":
        raise ValueError("unsupported manifest contract")
    release = Path(manifest["immutable_release_path"]).resolve()
    module = Path(manifest["module_path"]).resolve()
    expected_module_sha256 = manifest["module_sha256"]
    expected_tree_sha256 = manifest["package_tree_sha256"]
    canonical_registry_root = Path(manifest["canonical_registry_root"]).resolve()
    canonical_registry_inventory = Path(manifest["canonical_registry_inventory_path"]).resolve()
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"bureau runtime manifest invalid: {{exc}}")
if module.is_symlink() or not module.is_file():
    raise SystemExit("bureau runtime module is not a regular file")
if hashlib.sha256(module.read_bytes()).hexdigest() != expected_module_sha256:
    raise SystemExit("bureau runtime module digest mismatch")
try:
    module.relative_to(release)
except ValueError:
    raise SystemExit("bureau runtime module escaped immutable release")
pyproject = release / "pyproject.toml"
package = release / "src/bureau"
if pyproject.is_symlink() or not pyproject.is_file() or not package.is_dir():
    raise SystemExit("bureau runtime package tree is incomplete")
digest = hashlib.sha256()
for path in [pyproject, *sorted(package.rglob("*.py"))]:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("bureau runtime package tree contains a non-regular file")
    relative = path.relative_to(release).as_posix().encode()
    content = path.read_bytes()
    digest.update(len(relative).to_bytes(4, "big"))
    digest.update(relative)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)
if digest.hexdigest() != expected_tree_sha256:
    raise SystemExit("bureau runtime package tree digest mismatch")
if not canonical_registry_root.is_dir() or canonical_registry_root.is_symlink():
    raise SystemExit("bureau canonical Registry snapshot is invalid")
if not canonical_registry_inventory.is_file() or canonical_registry_inventory.is_symlink():
    raise SystemExit("bureau canonical Registry inventory is invalid")
sys.path.insert(0, str(release / "src"))
os.environ["BUREAU_RUNTIME_MANIFEST"] = str(manifest_path)
os.environ["BUREAU_RUNTIME_MANIFEST_SHA256"] = expected_manifest_sha256
os.environ["BUREAU_REGISTRY_ROOT"] = str(canonical_registry_root)
os.environ["BUREAU_REGISTRY_ROOT_MODE"] = "canonical-runtime-default"
os.environ.setdefault("BUREAU_JSON_ENVELOPE", "1")
from bureau.cli import main
raise SystemExit(main())
'''.encode()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--source", default=".")
    value.add_argument("--prefix", default="~/.local/share/bureau")
    value.add_argument("--bin-dir", default="~/.local/bin")
    value.add_argument("--replace-existing", action="store_true")
    return value


def _backup_existing(prefix: Path, manifest_path: Path, launcher: Path) -> dict[str, Any]:
    launcher_present = os.path.lexists(launcher)
    if not manifest_path.exists() and not launcher_present:
        return {
            "directory": None,
            "manifest": None,
            "launcher": None,
            "launcher_kind": None,
            "launcher_symlink_target": None,
            "launcher_metadata": None,
        }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = prefix / "backups" / stamp
    directory.mkdir(parents=True, exist_ok=False)
    manifest_backup = None
    launcher_backup = None
    launcher_kind = None
    launcher_symlink_target = None
    launcher_metadata = None
    if manifest_path.is_file() and not manifest_path.is_symlink():
        manifest_backup = directory / "deployment-manifest.json"
        shutil.copy2(manifest_path, manifest_backup)
    if launcher.is_symlink():
        launcher_kind = "symlink"
        launcher_symlink_target = os.readlink(launcher)
        launcher_metadata = directory / "bureau.symlink.json"
        atomic_write(
            launcher_metadata,
            canonical(
                {
                    "schema_version": 1,
                    "kind": "bureau_launcher_symlink_backup",
                    "path": str(launcher),
                    "target": launcher_symlink_target,
                }
            ),
        )
    elif launcher.is_file():
        launcher_kind = "file"
        launcher_backup = directory / "bureau"
        shutil.copy2(launcher, launcher_backup)
    return {
        "directory": str(directory),
        "manifest": str(manifest_backup) if manifest_backup else None,
        "launcher": str(launcher_backup) if launcher_backup else None,
        "launcher_kind": launcher_kind,
        "launcher_symlink_target": launcher_symlink_target,
        "launcher_metadata": str(launcher_metadata) if launcher_metadata else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    prefix = Path(args.prefix).expanduser().resolve()
    bin_dir = Path(args.bin_dir).expanduser().resolve()
    top = Path(git(source, "rev-parse", "--show-toplevel")).resolve()
    if top != source:
        raise SystemExit(f"source must be repository root: {top}")
    head = git(source, "rev-parse", "HEAD")
    origin_main = git(source, "rev-parse", "origin/main")
    status = git(source, "status", "--porcelain=v1", "--untracked-files=normal")
    if status:
        raise SystemExit("source checkout is dirty")
    if head != origin_main:
        raise SystemExit("source HEAD differs from origin/main")

    registry_snapshot = ensure_registry_snapshot(source, prefix, head)
    source_digest = package_tree_sha256(source)
    release_id = f"{head[:12]}-src{source_digest[:12]}"
    release = prefix / "releases" / release_id
    module = release / "src/bureau/runtime_identity.py"
    if not release.exists():
        release.parent.mkdir(parents=True, exist_ok=True)
        temporary = release.parent / f".{release_id}.tmp-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        (temporary / "src").mkdir(parents=True)
        shutil.copytree(source / "src/bureau", temporary / "src/bureau", symlinks=True)
        shutil.copy2(source / "pyproject.toml", temporary / "pyproject.toml", follow_symlinks=False)
        if package_tree_sha256(temporary) != source_digest:
            shutil.rmtree(temporary)
            raise SystemExit("copied Bureau package tree digest mismatch")
        for path in sorted(temporary.rglob("*"), reverse=True):
            if path.is_file():
                path.chmod(0o444)
            elif path.is_dir():
                path.chmod(0o555)
        temporary.chmod(0o555)
        os.replace(temporary, release)
    if package_tree_sha256(release) != source_digest:
        raise SystemExit("existing immutable release digest mismatch")
    if not module.is_file() or module.is_symlink():
        raise SystemExit("immutable release is missing runtime_identity.py")

    manifest_path = prefix / "deployment-manifest.json"
    launcher = bin_dir / "bureau"
    if manifest_path.exists() and (manifest_path.is_symlink() or not manifest_path.is_file()):
        raise SystemExit("existing Bureau runtime manifest is not a regular file")
    launcher_present = os.path.lexists(launcher)
    existing_launcher = (
        launcher.read_text(encoding="utf-8", errors="replace")
        if launcher.is_file() and not launcher.is_symlink()
        else None
    )
    if launcher.is_symlink():
        if not args.replace_existing:
            raise SystemExit("existing bureau launcher is a symlink; use --replace-existing")
    elif launcher_present and existing_launcher is None:
        raise SystemExit("existing bureau launcher is not a regular file or symlink")
    if (
        existing_launcher is not None
        and MANAGED_MARKER not in existing_launcher
        and not args.replace_existing
    ):
        raise SystemExit("existing bureau launcher is unmanaged; use --replace-existing")
    backup = _backup_existing(prefix, manifest_path, launcher)
    previous_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
    installed_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": 1,
        "kind": "bureau_runtime_deployment",
        "release_id": release_id,
        "source_repository": str(source),
        "source_commit": head,
        "package_tree_sha256": source_digest,
        "immutable_release_path": str(release),
        "module_path": str(module),
        "module_sha256": sha256(module),
        "canonical_registry_root": registry_snapshot["root"],
        "canonical_registry_inventory_path": registry_snapshot["inventory_path"],
        "canonical_registry_inventory_sha256": registry_snapshot["inventory_sha256"],
        "canonical_registry_tree_sha256": registry_snapshot["tree_sha256"],
        "launcher_path": str(launcher),
        "installed_at": installed_at,
        "previous_manifest_sha256": (
            hashlib.sha256(previous_manifest).hexdigest() if previous_manifest else None
        ),
        "rollback": backup,
    }
    manifest_bytes = canonical(manifest)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    launcher_bytes = wrapper(manifest_path, manifest_digest)

    atomic_write(manifest_path, manifest_bytes)
    atomic_write(launcher, launcher_bytes, 0o755)
    receipt = {
        "schema_version": 1,
        "kind": "bureau_runtime_install_receipt",
        "release_id": release_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "launcher_path": str(launcher),
        "launcher_sha256": sha256(launcher),
        "package_tree_sha256": source_digest,
        "canonical_registry_root": registry_snapshot["root"],
        "canonical_registry_tree_sha256": registry_snapshot["tree_sha256"],
        "rollback": backup,
        "installed_at": installed_at,
    }
    receipt_path = prefix / "receipts" / f"{release_id}-{manifest_digest[:12]}.json"
    atomic_write(receipt_path, canonical(receipt))
    print(json.dumps({**receipt, "receipt_path": str(receipt_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
