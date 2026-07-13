"""Observable Bureau runtime, registry and state identity.

The module separates source identity from Registry truth. An ambient package
must not silently gain write authority over a different Bureau checkout.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .registry_snapshot import canonical_registry_identity


def _sha256(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _package_tree_sha256(root: Path) -> str | None:
    pyproject = root / "pyproject.toml"
    package = root / "src/bureau"
    if pyproject.is_symlink() or not pyproject.is_file() or not package.is_dir():
        return None
    paths = [pyproject, *sorted(package.rglob("*.py"))]
    digest = hashlib.sha256()
    try:
        for path in paths:
            if path.is_symlink() or not path.is_file():
                return None
            relative = path.relative_to(root).as_posix().encode()
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def _git(root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip("\n")


def _git_identity(root: Path) -> dict[str, Any]:
    top = _git(root, "rev-parse", "--show-toplevel")
    if not top:
        return {
            "available": False,
            "root": str(root),
            "head": None,
            "origin_main": None,
            "head_equals_origin_main": None,
            "dirty": None,
            "dirty_paths": [],
        }
    resolved = Path(top).resolve()
    status = _git(resolved, "status", "--porcelain=v1", "--untracked-files=normal")
    dirty_paths: list[str] = []
    if status:
        for line in status.splitlines():
            if len(line) >= 4:
                dirty_paths.append(line[3:])
    head = _git(resolved, "rev-parse", "HEAD")
    origin_main = _git(resolved, "rev-parse", "origin/main")
    return {
        "available": True,
        "root": str(resolved),
        "head": head,
        "origin_main": origin_main,
        "head_equals_origin_main": bool(head and origin_main and head == origin_main),
        "dirty": bool(status),
        "dirty_paths": dirty_paths,
    }


def _is_bureau_project(root: Path) -> bool:
    return (root / "pyproject.toml").is_file() and (root / "src/bureau").is_dir()


def _source_root(module_path: Path) -> Path | None:
    for parent in module_path.parents:
        if _is_bureau_project(parent):
            return parent.resolve()
    return None


def _state_identity(state_path: Path | None) -> dict[str, Any]:
    if state_path is None:
        return {"available": False, "path": None, "schema_version": None}
    resolved = state_path.expanduser().resolve()
    if not resolved.is_file():
        return {"available": False, "path": str(resolved), "schema_version": None}
    try:
        connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        return {
            "available": False,
            "path": str(resolved),
            "schema_version": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if "connection" in locals():
            connection.close()
    return {
        "available": True,
        "path": str(resolved),
        "schema_version": version,
        "integrity": integrity,
    }


def _manifest_path() -> Path:
    configured = os.environ.get("BUREAU_RUNTIME_MANIFEST")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local/share/bureau/deployment-manifest.json"


def _manifest_identity(module_path: Path) -> dict[str, Any]:
    configured_path = _manifest_path()
    if configured_path.is_symlink():
        return {
            "available": True,
            "valid": False,
            "path": str(configured_path),
            "reason": "manifest-symlink",
        }
    path = configured_path.resolve()
    if not path.is_file():
        return {"available": False, "valid": False, "path": str(path), "reason": "missing"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["schema_version"] != 1 or value["kind"] != "bureau_runtime_deployment":
            raise ValueError("unsupported manifest contract")
        release = Path(value["immutable_release_path"]).expanduser().resolve()
        expected_module = Path(value["module_path"]).expanduser().resolve()
        expected_module_sha256 = value["module_sha256"]
        expected_tree_sha256 = value["package_tree_sha256"]
        source_commit = value["source_commit"]
        release_id = value["release_id"]
        canonical_registry = canonical_registry_identity(value)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "available": True,
            "valid": False,
            "path": str(path),
            "reason": f"malformed:{type(exc).__name__}",
        }
    reasons: list[str] = []
    if module_path != expected_module:
        reasons.append("module-path-mismatch")
    try:
        module_path.relative_to(release)
    except ValueError:
        reasons.append("module-outside-release")
    actual_module_sha256 = _sha256(module_path)
    if actual_module_sha256 != expected_module_sha256:
        reasons.append("module-digest-mismatch")
    actual_tree_sha256 = _package_tree_sha256(release)
    if actual_tree_sha256 != expected_tree_sha256:
        reasons.append("package-tree-digest-mismatch")
    if canonical_registry.get("available") and not canonical_registry.get("valid"):
        reasons.extend(f"canonical-registry-{item}" for item in canonical_registry["reasons"])
    return {
        "available": True,
        "valid": not reasons,
        "path": str(path),
        "sha256": _sha256(path),
        "release_id": release_id,
        "immutable_release_path": str(release),
        "source_commit": source_commit,
        "module_path": str(expected_module),
        "module_sha256": expected_module_sha256,
        "observed_module_sha256": actual_module_sha256,
        "package_tree_sha256": expected_tree_sha256,
        "observed_package_tree_sha256": actual_tree_sha256,
        "canonical_registry": canonical_registry,
        "reasons": reasons,
    }


def _package_versions() -> dict[str, str | None]:
    from . import __version__

    try:
        distribution = importlib.metadata.version("heimgewebe-bureau")
    except importlib.metadata.PackageNotFoundError:
        distribution = None
    return {"package_version": __version__, "distribution_version": distribution}


def bureau_runtime_identity(
    registry_root: Path,
    *,
    state_path: Path | None = None,
    module_path: Path | None = None,
) -> dict[str, Any]:
    """Return source/Registry identity and a fail-closed mutation verdict."""
    observed_module = (module_path or Path(__file__)).expanduser().resolve()
    resolved_registry_root = registry_root.expanduser().resolve()
    registry = _git_identity(resolved_registry_root)
    managed_registry = _is_bureau_project(resolved_registry_root)
    registry["bureau_project"] = managed_registry
    source_root = _source_root(observed_module)
    source = (
        _git_identity(source_root)
        if source_root
        else {
            "available": False,
            "root": None,
            "head": None,
            "origin_main": None,
            "head_equals_origin_main": None,
            "dirty": None,
            "dirty_paths": [],
        }
    )
    manifest = _manifest_identity(observed_module)
    reasons: list[str] = []
    source_kind = "unbound"
    canonical_registry = manifest.get("canonical_registry", {})
    canonical_selected = bool(
        canonical_registry.get("valid") is True
        and canonical_registry.get("root")
        and Path(canonical_registry["root"]) == resolved_registry_root
    )

    if canonical_selected:
        managed_registry = True
        registry.update(
            {
                "available": True,
                "root": str(resolved_registry_root),
                "head": canonical_registry.get("source_commit"),
                "origin_main": canonical_registry.get("source_commit"),
                "head_equals_origin_main": True,
                "dirty": False,
                "dirty_paths": [],
                "bureau_project": True,
                "role": "canonical-runtime-snapshot",
                "snapshot_tree_sha256": canonical_registry.get("tree_sha256"),
            }
        )

    if canonical_selected:
        status = "canonical-read-only"
        mutation_allowed = False
        source_kind = "immutable-release"
        reasons.append("canonical-registry-read-only")
    elif registry["available"] is not True or not managed_registry:
        status = "unmanaged-registry"
        mutation_allowed = True
        source_kind = "development"
        reason = (
            "registry-not-git"
            if registry["available"] is not True
            else "registry-not-bureau-project"
        )
        reasons.append(reason)
    elif source_root and Path(registry["root"]) == source_root:
        source_kind = "same-checkout"
        if source.get("dirty"):
            status = "dirty"
            mutation_allowed = False
            reasons.append("source-checkout-dirty")
        else:
            status = "compatible"
            mutation_allowed = True
    elif manifest.get("valid") is True:
        source_kind = "immutable-release"
        if manifest.get("source_commit") == registry.get("head") and registry.get("dirty") is False:
            status = "compatible"
            mutation_allowed = True
        else:
            status = "stale"
            mutation_allowed = False
            reasons.append("release-registry-identity-mismatch")
    else:
        status = "unbound"
        mutation_allowed = False
        reasons.append("runtime-not-bound-to-registry")
        if manifest.get("available"):
            reasons.extend(str(item) for item in manifest.get("reasons", []))

    return {
        "schema_version": 1,
        "kind": "bureau_runtime_identity",
        "module": {
            "path": str(observed_module),
            "sha256": _sha256(observed_module),
            "source_kind": source_kind,
            **_package_versions(),
        },
        "source": source,
        "registry": registry,
        "state": _state_identity(state_path),
        "manifest": manifest,
        "compatibility": {
            "status": status,
            "mutation_allowed": mutation_allowed,
            "reason_codes": sorted(set(reasons)),
        },
        "does_not_establish": [
            "registry_semantic_correctness",
            "task_or_queue_authority",
            "runtime_health_beyond_observed_files",
            "future_command_success",
        ],
    }


def require_mutation_compatible(identity: dict[str, Any]) -> dict[str, Any] | None:
    compatibility = identity.get("compatibility", {})
    if compatibility.get("mutation_allowed") is True:
        return None
    return {
        "schema_version": 1,
        "status": "stale-runtime-blocked",
        "reason_codes": compatibility.get("reason_codes", []),
        "runtime_identity": identity,
        "does_not_establish": ["mutation_authority", "registry_corruption", "safe_retry"],
    }
