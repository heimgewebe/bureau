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
import sys
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


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        fsync_directory(path.parent)
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


MANAGED_PACKAGES = ("bureau", "bureau_cycle")
SCHEDULER_NAMES = (
    "bureau-halfhour-operator",
    "bureau-curator",
    "bureau-operator-control",
    "bureau-verifier-control",
    "bureau-closure-planner",
    "bureau-task-supply",
)


def scheduler_fragment_paths(root: Path) -> list[Path]:
    systemd = root / "ops/systemd"
    return [
        *(systemd / f"{name}.service" for name in SCHEDULER_NAMES),
        *(systemd / f"{name}.timer" for name in SCHEDULER_NAMES),
        *(systemd / "libexec" / name for name in SCHEDULER_NAMES),
    ]


def package_source_paths(root: Path) -> list[Path]:
    pyproject = root / "pyproject.toml"
    packages = [root / "src" / name for name in MANAGED_PACKAGES]
    schemas = root / "schemas"
    systemd = root / "ops/systemd"
    schema_paths = sorted(schemas.glob("*.json")) if schemas.is_dir() else []
    if (
        pyproject.is_symlink()
        or not pyproject.is_file()
        or schemas.is_symlink()
        or not schemas.is_dir()
        or not schema_paths
        or systemd.is_symlink()
        or not systemd.is_dir()
        or any(package.is_symlink() or not package.is_dir() for package in packages)
    ):
        raise SystemExit(f"invalid Bureau package tree: {root}")
    paths = [
        pyproject,
        *schema_paths,
        *(path for package in packages for path in sorted(package.rglob("*.py"))),
        *scheduler_fragment_paths(root),
    ]
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"package tree contains non-regular input: {path}")
    return paths


def package_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in package_source_paths(root):
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(b"x" if path.stat().st_mode & 0o111 else b"-")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def copy_managed_package(source: Path, destination: Path) -> None:
    for source_path in package_source_paths(source):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path, follow_symlinks=False)


def validate_managed_release_tree(root: Path) -> None:
    managed_roots = [
        *(root / "src" / name for name in MANAGED_PACKAGES),
        root / "schemas",
        root / "ops/systemd",
    ]
    expected = {path.resolve() for path in package_source_paths(root)}
    for package in managed_roots:
        for entry in sorted(package.rglob("*")):
            try:
                linked = entry.lstat()
                resolved = entry.resolve(strict=True)
            except OSError as exc:
                raise SystemExit(
                    f"immutable release contains unavailable entry: {entry}: {type(exc).__name__}"
                ) from None
            if entry.is_symlink() or resolved != entry or not resolved.is_relative_to(root):
                raise SystemExit(f"immutable release contains unsafe entry: {entry}")
            if stat.S_ISDIR(linked.st_mode):
                continue
            if not stat.S_ISREG(linked.st_mode) or resolved not in expected:
                raise SystemExit(f"immutable release contains unmanaged entry: {entry}")


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


def wrapper(
    manifest_path: Path,
    entrypoint: str = "bureau.cli",
) -> bytes:
    from bureau.runtime_refresh import stable_launcher_bytes

    return stable_launcher_bytes(manifest_path, entrypoint)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--source", default=".")
    value.add_argument("--prefix", default="~/.local/share/bureau")
    value.add_argument("--bin-dir", default="~/.local/bin")
    value.add_argument("--user-unit-dir", default="~/.config/systemd/user")
    value.add_argument("--libexec-dir", default="~/.local/libexec")
    value.add_argument("--converge-user-systemd", action="store_true")
    value.add_argument("--approval-intent")
    value.add_argument(
        "--runtime-refresh-state-root",
        default=os.environ.get(
            "BUREAU_RUNTIME_REFRESH_STATE_ROOT",
            "~/.local/state/bureau/runtime-refresh",
        ),
    )
    value.add_argument(
        "--resource-db",
        default=os.environ.get(
            "GRABOWSKI_RESOURCE_DB",
            "~/.local/state/grabowski/resources.sqlite3",
        ),
    )
    value.add_argument("--replace-existing", action="store_true")
    value.add_argument("--enforce-launcher-allowlist", action="store_true")
    value.add_argument("--allowed-launcher-path", action="append", default=[])
    return value


def _backup_launcher(directory: Path, launcher: Path, label: str) -> dict[str, Any]:
    backup = None
    kind = None
    symlink_target = None
    metadata = None
    if launcher.is_symlink():
        kind = "symlink"
        symlink_target = os.readlink(launcher)
        metadata = directory / f"{label}.symlink.json"
        atomic_write(
            metadata,
            canonical(
                {
                    "schema_version": 1,
                    "kind": "bureau_launcher_symlink_backup",
                    "path": str(launcher),
                    "target": symlink_target,
                }
            ),
        )
    elif launcher.is_file():
        kind = "file"
        backup = directory / label
        shutil.copy2(launcher, backup)
    return {
        "path": str(backup) if backup else None,
        "kind": kind,
        "symlink_target": symlink_target,
        "metadata": str(metadata) if metadata else None,
    }


def _backup_existing(
    prefix: Path,
    manifest_path: Path,
    launcher: Path,
    runtime_refresh_launcher: Path | None = None,
    status_capsule_launcher: Path | None = None,
    force_generation: bool = False,
) -> dict[str, Any]:
    launchers = [launcher]
    if runtime_refresh_launcher is not None:
        launchers.append(runtime_refresh_launcher)
    if status_capsule_launcher is not None:
        launchers.append(status_capsule_launcher)
    if (
        not force_generation
        and not manifest_path.exists()
        and not any(os.path.lexists(item) for item in launchers)
    ):
        return {
            "directory": None,
            "manifest": None,
            "launcher": None,
            "launcher_kind": None,
            "launcher_symlink_target": None,
            "launcher_metadata": None,
            "runtime_refresh_launcher": None,
            "runtime_refresh_launcher_kind": None,
            "runtime_refresh_launcher_symlink_target": None,
            "runtime_refresh_launcher_metadata": None,
            "status_capsule_launcher": None,
            "status_capsule_launcher_kind": None,
            "status_capsule_launcher_symlink_target": None,
            "status_capsule_launcher_metadata": None,
        }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = prefix / "backups" / stamp
    directory.mkdir(parents=True, exist_ok=False)
    manifest_backup = None
    if manifest_path.is_file() and not manifest_path.is_symlink():
        manifest_backup = directory / "deployment-manifest.json"
        shutil.copy2(manifest_path, manifest_backup)
    primary = _backup_launcher(directory, launcher, "bureau")
    refresh = (
        _backup_launcher(directory, runtime_refresh_launcher, "bureau-runtime-refresh")
        if runtime_refresh_launcher is not None
        else {"path": None, "kind": None, "symlink_target": None, "metadata": None}
    )
    status_capsule = (
        _backup_launcher(directory, status_capsule_launcher, "bureau-status-capsule")
        if status_capsule_launcher is not None
        else {"path": None, "kind": None, "symlink_target": None, "metadata": None}
    )
    return {
        "directory": str(directory),
        "manifest": str(manifest_backup) if manifest_backup else None,
        "launcher": primary["path"],
        "launcher_kind": primary["kind"],
        "launcher_symlink_target": primary["symlink_target"],
        "launcher_metadata": primary["metadata"],
        "runtime_refresh_launcher": refresh["path"],
        "runtime_refresh_launcher_kind": refresh["kind"],
        "runtime_refresh_launcher_symlink_target": refresh["symlink_target"],
        "runtime_refresh_launcher_metadata": refresh["metadata"],
        "status_capsule_launcher": status_capsule["path"],
        "status_capsule_launcher_kind": status_capsule["kind"],
        "status_capsule_launcher_symlink_target": status_capsule["symlink_target"],
        "status_capsule_launcher_metadata": status_capsule["metadata"],
    }


def _validate_existing_launcher(launcher: Path, *, label: str, replace_existing: bool) -> None:
    present = os.path.lexists(launcher)
    existing = (
        launcher.read_text(encoding="utf-8", errors="replace")
        if launcher.is_file() and not launcher.is_symlink()
        else None
    )
    if launcher.is_symlink():
        if not replace_existing:
            raise SystemExit(f"existing {label} launcher is a symlink; use --replace-existing")
    elif present and existing is None:
        raise SystemExit(f"existing {label} launcher is not a regular file or symlink")
    if existing is not None and MANAGED_MARKER not in existing and not replace_existing:
        raise SystemExit(f"existing {label} launcher is unmanaged; use --replace-existing")


def _launcher_needs_write(path: Path, expected: bytes) -> bool:
    try:
        return not (
            not path.is_symlink()
            and path.is_file()
            and bool(path.stat().st_mode & 0o111)
            and path.read_bytes() == expected
        )
    except OSError:
        return True


def _write_launcher_if_needed(
    path: Path,
    expected: bytes,
    *,
    enforce_allowlist: bool,
    allowed_paths: set[Path],
) -> bool:
    if not _launcher_needs_write(path, expected):
        return False
    if enforce_allowlist and path not in allowed_paths:
        raise RuntimeError(f"launcher mutation is not covered by runtime-refresh lease: {path}")
    atomic_write(path, expected, 0o755)
    return True


def _restore_symlink(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.restore-{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _restore_install_preimage(
    *,
    backup: dict[str, Any],
    manifest_path: Path,
    launchers: dict[str, Path],
    receipt_path: Path | None,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []

    def attempt(label: str, operation: Any) -> None:
        try:
            operation()
        except Exception as exc:
            failures.append({"operation": ["restore", label], "error": repr(exc)})

    def restore_file(target: Path, backup_path: str | None) -> None:
        if backup_path is None:
            if target.is_dir() and not target.is_symlink():
                raise OSError(f"rollback target became a directory: {target}")
            if os.path.lexists(target):
                target.unlink()
                fsync_directory(target.parent)
            return
        source = Path(backup_path)
        if source.is_symlink() or not source.is_file():
            raise OSError(f"rollback backup is unavailable: {source}")
        atomic_write(target, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))

    attempt("manifest", lambda: restore_file(manifest_path, backup.get("manifest")))
    for label, target in launchers.items():
        key = "launcher" if label == "bureau" else f"{label.replace('-', '_')}_launcher"
        kind = backup.get(f"{key}_kind")
        if kind == "symlink":
            attempt(
                label,
                lambda target=target, key=key: _restore_symlink(
                    target, str(backup[f"{key}_symlink_target"])
                ),
            )
        elif kind in {"file", None}:
            attempt(
                label,
                lambda target=target, key=key: restore_file(target, backup.get(key)),
            )
        else:
            failures.append(
                {"operation": ["restore", label], "error": f"invalid backup kind: {kind}"}
            )
    if receipt_path is not None:
        attempt("receipt", lambda: restore_file(receipt_path, None))

    expected: list[tuple[str, Path, str | None, str | None]] = [
        (
            "manifest",
            manifest_path,
            "file" if backup.get("manifest") else None,
            backup.get("manifest"),
        )
    ]
    for label, target in launchers.items():
        key = "launcher" if label == "bureau" else f"{label.replace('-', '_')}_launcher"
        expected.append((label, target, backup.get(f"{key}_kind"), backup.get(key)))
    for label, target, kind, backup_path in expected:
        try:
            if kind is None:
                matches = not os.path.lexists(target)
            elif kind == "symlink":
                key = "launcher" if label == "bureau" else f"{label.replace('-', '_')}_launcher"
                matches = target.is_symlink() and os.readlink(target) == backup.get(
                    f"{key}_symlink_target"
                )
            else:
                source = Path(str(backup_path))
                matches = (
                    not target.is_symlink()
                    and target.is_file()
                    and target.read_bytes() == source.read_bytes()
                    and stat.S_IMODE(target.stat().st_mode)
                    == stat.S_IMODE(source.stat().st_mode)
                )
            if not matches:
                failures.append({"operation": ["verify-preimage", label]})
        except Exception as exc:
            failures.append(
                {"operation": ["verify-preimage", label], "error": repr(exc)}
            )
    if receipt_path is not None and os.path.lexists(receipt_path):
        failures.append({"operation": ["verify-preimage", "receipt"]})
    return failures


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    prefix = Path(args.prefix).expanduser().resolve()
    bin_dir = Path(args.bin_dir).expanduser().resolve()
    user_unit_dir = Path(args.user_unit_dir).expanduser().resolve()
    libexec_dir = Path(args.libexec_dir).expanduser().resolve()
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
    package_root = source / "src"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from bureau.runtime_refresh import (
        RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD,
        RuntimeRefreshError,
        converge_user_scheduler,
        read_json,
        scheduler_resource_keys,
        stable_launcher_bytes,
        validate_legacy_runtime_refresh_bootstrap,
        validate_runtime_approval_intent,
    )

    manifest_path = prefix / "deployment-manifest.json"
    try:
        if args.approval_intent:
            runtime_approval = validate_runtime_approval_intent(
                Path(args.approval_intent).expanduser().resolve(),
                expected_source_commit=head,
            )
        else:
            runtime_approval = validate_legacy_runtime_refresh_bootstrap(
                state_root=Path(args.runtime_refresh_state_root).expanduser().resolve(),
                resource_db=Path(args.resource_db).expanduser().resolve(),
                expected_source_commit=head,
                prefix=prefix,
                bin_dir=bin_dir,
                manifest_path=manifest_path,
            )
    except RuntimeRefreshError as exc:
        raise SystemExit(f"runtime approval denied: {exc.code}: {exc.message}") from exc
    if args.converge_user_systemd:
        if not args.approval_intent:
            raise SystemExit("scheduler convergence requires a typed runtime-refresh intent")
        approval_intent = read_json(Path(args.approval_intent).expanduser().resolve())
        expected_scheduler_keys = set(
            scheduler_resource_keys(
                user_unit_dir=user_unit_dir,
                libexec_dir=libexec_dir,
            )
        )
        intent_resource_keys = approval_intent.get("required_resource_keys")
        forbidden_scheduler_keys = {
            f"path:{user_unit_dir}",
            f"path:{libexec_dir}",
        }
        if (
            approval_intent.get("user_unit_dir") != str(user_unit_dir)
            or approval_intent.get("libexec_dir") != str(libexec_dir)
            or not isinstance(intent_resource_keys, list)
            or not all(isinstance(item, str) for item in intent_resource_keys)
            or not expected_scheduler_keys.issubset(set(intent_resource_keys))
            or not forbidden_scheduler_keys.isdisjoint(intent_resource_keys)
        ):
            raise SystemExit(
                "scheduler convergence paths are not covered by the runtime-refresh intent"
            )

    launcher = bin_dir / "bureau"
    runtime_refresh_launcher = bin_dir / "bureau-runtime-refresh"
    status_capsule_launcher = bin_dir / "bureau-status-capsule"
    expected_launchers = {
        launcher: stable_launcher_bytes(manifest_path),
        runtime_refresh_launcher: stable_launcher_bytes(
            manifest_path, "bureau.runtime_refresh"
        ),
        status_capsule_launcher: stable_launcher_bytes(
            manifest_path, "bureau.status_capsule"
        ),
    }
    # Normalize lexically, not with Path.resolve(): a launcher may itself
    # be a symlink that this exact leased path is authorized to replace.
    allowed_launcher_paths = {
        Path(os.path.abspath(os.path.expanduser(value)))
        for value in args.allowed_launcher_path
    }
    initial_launcher_mutations = {
        path
        for path, expected in expected_launchers.items()
        if _launcher_needs_write(path, expected)
    }
    if args.enforce_launcher_allowlist:
        unleased = sorted(
            str(path) for path in initial_launcher_mutations if path not in allowed_launcher_paths
        )
        if unleased:
            raise SystemExit(
                "launcher mutation is not covered by runtime-refresh lease: " + ", ".join(unleased)
            )

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
        copy_managed_package(source, temporary)
        validate_managed_release_tree(temporary)
        if package_tree_sha256(temporary) != source_digest:
            shutil.rmtree(temporary)
            raise SystemExit("copied Bureau package tree digest mismatch")
        for path in sorted(temporary.rglob("*"), reverse=True):
            if path.is_file():
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
            elif path.is_dir():
                path.chmod(0o555)
        temporary.chmod(0o555)
        os.replace(temporary, release)
    validate_managed_release_tree(release)
    if package_tree_sha256(release) != source_digest:
        raise SystemExit("existing immutable release digest mismatch")
    if not module.is_file() or module.is_symlink():
        raise SystemExit("immutable release is missing runtime_identity.py")

    if manifest_path.exists() and (manifest_path.is_symlink() or not manifest_path.is_file()):
        raise SystemExit("existing Bureau runtime manifest is not a regular file")
    _validate_existing_launcher(launcher, label="bureau", replace_existing=args.replace_existing)
    _validate_existing_launcher(
        runtime_refresh_launcher,
        label="bureau-runtime-refresh",
        replace_existing=args.replace_existing,
    )
    _validate_existing_launcher(
        status_capsule_launcher,
        label="bureau-status-capsule",
        replace_existing=args.replace_existing,
    )
    backup = _backup_existing(
        prefix,
        manifest_path,
        launcher,
        runtime_refresh_launcher,
        status_capsule_launcher,
        force_generation=args.converge_user_systemd,
    )
    previous_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
    installed_at = datetime.now(timezone.utc).isoformat()
    launcher_bytes = expected_launchers[launcher]
    runtime_refresh_launcher_bytes = expected_launchers[runtime_refresh_launcher]
    status_capsule_launcher_bytes = expected_launchers[status_capsule_launcher]

    def build_manifest(scheduler: dict[str, Any] | None = None) -> tuple[dict[str, Any], bytes]:
        value = {
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
            "runtime_refresh_launcher_path": str(runtime_refresh_launcher),
            "status_capsule_launcher_path": str(status_capsule_launcher),
            "installed_at": installed_at,
            "runtime_approval": runtime_approval,
            "previous_manifest_sha256": (
                hashlib.sha256(previous_manifest).hexdigest() if previous_manifest else None
            ),
            "rollback": backup,
        }
        if scheduler is not None:
            value["scheduler"] = scheduler
        value[RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD] = hashlib.sha256(
            canonical(value)
        ).hexdigest()
        return value, canonical(value)

    def validate_launcher_effect() -> None:
        final_launcher_mutations = {
            path
            for path, expected in expected_launchers.items()
            if _launcher_needs_write(path, expected)
        }
        if args.enforce_launcher_allowlist:
            unleased = sorted(
                str(path)
                for path in final_launcher_mutations
                if path not in allowed_launcher_paths
            )
            if unleased:
                raise RuntimeRefreshError(
                    "launcher-drift-before-effect",
                    "launcher drift detected before effect without runtime-refresh lease",
                    details={"paths": unleased},
                )

    transaction: dict[str, Any] = {
        "receipt": None,
        "receipt_path": None,
        "reported": False,
        "launcher_written": False,
        "runtime_refresh_launcher_written": False,
        "status_capsule_launcher_written": False,
    }

    def write_candidate_install() -> None:
        validate_launcher_effect()
        _candidate_manifest, candidate_bytes = build_manifest()
        atomic_write(manifest_path, candidate_bytes)
        transaction["launcher_written"] = _write_launcher_if_needed(
            launcher,
            launcher_bytes,
            enforce_allowlist=args.enforce_launcher_allowlist,
            allowed_paths=allowed_launcher_paths,
        )
        transaction["runtime_refresh_launcher_written"] = _write_launcher_if_needed(
            runtime_refresh_launcher,
            runtime_refresh_launcher_bytes,
            enforce_allowlist=args.enforce_launcher_allowlist,
            allowed_paths=allowed_launcher_paths,
        )
        transaction["status_capsule_launcher_written"] = _write_launcher_if_needed(
            status_capsule_launcher,
            status_capsule_launcher_bytes,
            enforce_allowlist=args.enforce_launcher_allowlist,
            allowed_paths=allowed_launcher_paths,
        )

    def write_final_install(scheduler: dict[str, Any]) -> None:
        _manifest, final_manifest_bytes = build_manifest(scheduler)
        manifest_digest = hashlib.sha256(final_manifest_bytes).hexdigest()
        receipt_path = prefix / "receipts" / f"{release_id}-{manifest_digest[:12]}.json"
        if os.path.lexists(receipt_path):
            raise RuntimeRefreshError(
                "install-receipt-preexists",
                "durable install receipt target already exists",
                details={"path": str(receipt_path)},
            )
        receipt = {
            "schema_version": 1,
            "kind": "bureau_runtime_install_receipt",
            "release_id": release_id,
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_digest,
            "launcher_path": str(launcher),
            "launcher_sha256": hashlib.sha256(launcher_bytes).hexdigest(),
            "launcher_written": transaction["launcher_written"],
            "runtime_refresh_launcher_path": str(runtime_refresh_launcher),
            "runtime_refresh_launcher_sha256": hashlib.sha256(
                runtime_refresh_launcher_bytes
            ).hexdigest(),
            "runtime_refresh_launcher_written": transaction[
                "runtime_refresh_launcher_written"
            ],
            "status_capsule_launcher_path": str(status_capsule_launcher),
            "status_capsule_launcher_sha256": hashlib.sha256(
                status_capsule_launcher_bytes
            ).hexdigest(),
            "status_capsule_launcher_written": transaction[
                "status_capsule_launcher_written"
            ],
            "package_tree_sha256": source_digest,
            "canonical_registry_root": registry_snapshot["root"],
            "canonical_registry_tree_sha256": registry_snapshot["tree_sha256"],
            "rollback": backup,
            "runtime_approval": runtime_approval,
            "installed_at": installed_at,
            "scheduler": scheduler,
        }
        transaction["receipt_path"] = receipt_path
        atomic_write(manifest_path, final_manifest_bytes)
        atomic_write(receipt_path, canonical(receipt))
        for path, expected in expected_launchers.items():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.read_bytes() != expected
                or not path.stat().st_mode & 0o111
            ):
                raise RuntimeRefreshError(
                    "install-launcher-readback-failed",
                    "stable launcher changed before durable install completion",
                    details={"path": str(path)},
                )
        if sha256(manifest_path) != manifest_digest or receipt_path.read_bytes() != canonical(
            receipt
        ):
            raise RuntimeRefreshError(
                "install-durable-readback-failed",
                "manifest or durable receipt readback differs from the committed install",
            )
        transaction["receipt"] = receipt
        print(json.dumps({**receipt, "receipt_path": str(receipt_path)}, sort_keys=True))
        transaction["reported"] = True

    def rollback_install() -> list[dict[str, Any]]:
        return _restore_install_preimage(
            backup=backup,
            manifest_path=manifest_path,
            launchers={
                "bureau": launcher,
                "runtime-refresh": runtime_refresh_launcher,
                "status-capsule": status_capsule_launcher,
            },
            receipt_path=transaction["receipt_path"],
        )

    if args.converge_user_systemd:
        rollback_directory = backup.get("directory")
        if not isinstance(rollback_directory, str):
            raise SystemExit("scheduler convergence requires a rollback generation")
        try:
            converge_user_scheduler(
                source_commit=head,
                release_id=release_id,
                release=release,
                user_unit_dir=user_unit_dir,
                libexec_dir=libexec_dir,
                rollback_directory=Path(rollback_directory),
                manifest_path=manifest_path,
                before_validation=write_candidate_install,
                after_activation=write_final_install,
                rollback_effect=rollback_install,
            )
        except RuntimeRefreshError as exc:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "bureau_runtime_install_error",
                        "error": exc.as_dict(),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
    else:
        write_candidate_install()
        _manifest, manifest_bytes = build_manifest()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        atomic_write(manifest_path, manifest_bytes)
        receipt = {
            "schema_version": 1,
            "kind": "bureau_runtime_install_receipt",
            "release_id": release_id,
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_digest,
            "launcher_path": str(launcher),
            "launcher_sha256": sha256(launcher),
            "launcher_written": transaction["launcher_written"],
            "runtime_refresh_launcher_path": str(runtime_refresh_launcher),
            "runtime_refresh_launcher_sha256": sha256(runtime_refresh_launcher),
            "runtime_refresh_launcher_written": transaction[
                "runtime_refresh_launcher_written"
            ],
            "status_capsule_launcher_path": str(status_capsule_launcher),
            "status_capsule_launcher_sha256": sha256(status_capsule_launcher),
            "status_capsule_launcher_written": transaction[
                "status_capsule_launcher_written"
            ],
            "package_tree_sha256": source_digest,
            "canonical_registry_root": registry_snapshot["root"],
            "canonical_registry_tree_sha256": registry_snapshot["tree_sha256"],
            "rollback": backup,
            "runtime_approval": runtime_approval,
            "installed_at": installed_at,
        }
        receipt_path = prefix / "receipts" / f"{release_id}-{manifest_digest[:12]}.json"
        atomic_write(receipt_path, canonical(receipt))
        transaction.update({"receipt": receipt, "receipt_path": receipt_path})

    receipt = transaction["receipt"]
    receipt_path = transaction["receipt_path"]
    if not isinstance(receipt, dict) or not isinstance(receipt_path, Path):
        raise SystemExit("installer transaction completed without a durable receipt")
    if not transaction["reported"]:
        print(json.dumps({**receipt, "receipt_path": str(receipt_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
