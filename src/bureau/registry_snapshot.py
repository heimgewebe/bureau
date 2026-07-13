"""Hash-bound immutable Registry snapshots for the deployed Bureau runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot_tree_sha256(root: Path, paths: list[Path]) -> str | None:
    digest = hashlib.sha256()
    try:
        for relative in paths:
            path = root / relative
            if path.is_symlink() or not path.is_file():
                return None
            encoded = relative.as_posix().encode()
            content = path.read_bytes()
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except OSError:
        return None
    return digest.hexdigest()


def canonical_registry_identity(value: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "canonical_registry_root",
        "canonical_registry_inventory_path",
        "canonical_registry_inventory_sha256",
        "canonical_registry_tree_sha256",
    )
    if not any(field in value for field in fields):
        return {
            "available": False,
            "valid": False,
            "root": None,
            "reason": "not-configured",
            "reasons": [],
        }
    reasons: list[str] = []
    try:
        root = Path(value["canonical_registry_root"]).expanduser().resolve()
        inventory = Path(value["canonical_registry_inventory_path"]).expanduser().resolve()
        expected_inventory_sha256 = str(value["canonical_registry_inventory_sha256"])
        expected_tree_sha256 = str(value["canonical_registry_tree_sha256"])
    except (KeyError, TypeError, ValueError):
        return {
            "available": True,
            "valid": False,
            "root": None,
            "reason": "malformed",
            "reasons": ["malformed"],
        }
    observed_inventory_sha256 = _sha256(inventory)
    if root.is_symlink() or not root.is_dir():
        reasons.append("root-invalid")
    try:
        inventory.relative_to(root)
    except ValueError:
        reasons.append("inventory-outside-root")
    if observed_inventory_sha256 != expected_inventory_sha256:
        reasons.append("inventory-digest-mismatch")
    paths: list[Path] = []
    source_commit = None
    inventory_tree_sha256 = None
    if not reasons:
        try:
            payload = json.loads(inventory.read_text(encoding="utf-8"))
            if payload["schema_version"] != 1 or payload["kind"] != "bureau_registry_snapshot":
                raise ValueError("unsupported snapshot inventory")
            source_commit = str(payload["source_commit"])
            inventory_tree_sha256 = str(payload["tree_sha256"])
            for item in payload["paths"]:
                relative = Path(str(item))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe snapshot path")
                paths.append(relative)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            reasons.append("inventory-malformed")
    observed_tree_sha256 = snapshot_tree_sha256(root, paths) if paths else None
    if inventory_tree_sha256 is not None and inventory_tree_sha256 != expected_tree_sha256:
        reasons.append("inventory-tree-digest-mismatch")
    if observed_tree_sha256 != expected_tree_sha256:
        reasons.append("tree-digest-mismatch")
    if source_commit is not None and source_commit != value.get("source_commit"):
        reasons.append("source-commit-mismatch")
    return {
        "available": True,
        "valid": not reasons,
        "root": str(root),
        "inventory_path": str(inventory),
        "inventory_sha256": expected_inventory_sha256,
        "observed_inventory_sha256": observed_inventory_sha256,
        "tree_sha256": expected_tree_sha256,
        "observed_tree_sha256": observed_tree_sha256,
        "source_commit": source_commit,
        "reasons": sorted(set(reasons)),
    }
