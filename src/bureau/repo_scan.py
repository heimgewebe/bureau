from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .legacy import Resource, ValidationError

DEFAULT_DISCOVERY_REGISTRY = Path(
    os.environ.get(
        "BUREAU_DISCOVERY_REGISTRY",
        "~/.local/state/bureau-halfhour-operator/source-registry.json",
    )
).expanduser()

_MAX_REPOSITORIES = 512
_MAX_DISCOVERY_REPOSITORIES = 512
_MAX_PLANNING_REFERENCES = 512
_GIT_TIMEOUT_SECONDS = 3.0


def _normalized_path(value: str | Path) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))


def _normalized_github_slug(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("git@github.com:"):
        candidate = candidate.removeprefix("git@github.com:")
    elif candidate.startswith("ssh://") or candidate.startswith("http://") or candidate.startswith(
        "https://"
    ):
        parsed = urlparse(candidate)
        if (parsed.hostname or "").casefold() != "github.com":
            return None
        candidate = parsed.path.lstrip("/")
    elif candidate.startswith("github.com/"):
        candidate = candidate.removeprefix("github.com/")
    candidate = candidate.rstrip("/")
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    parts = candidate.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return "/".join(parts).casefold()


def _git_env() -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _git_read(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"read-only git probe failed for {repo}: {exc}") from exc


def _inspect_repository(path: str | None) -> dict[str, Any]:
    if not path:
        return {"state": "missing", "head_revision": None}
    repo = Path(path).expanduser()
    if not repo.exists():
        return {"state": "missing", "head_revision": None}
    if not repo.is_dir():
        return {"state": "not-git-repository", "head_revision": None}

    top = _git_read(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return {"state": "not-git-repository", "head_revision": None}
    reported_root = top.stdout.strip()
    if not reported_root or _normalized_path(reported_root) != _normalized_path(repo):
        return {"state": "not-git-repository", "head_revision": None}

    head = _git_read(repo, "rev-parse", "--verify", "HEAD^{commit}")
    revision = head.stdout.strip() if head.returncode == 0 else None
    if revision is not None:
        lowered = revision.casefold()
        if len(lowered) != 40 or any(character not in "0123456789abcdef" for character in lowered):
            raise ValidationError(f"unexpected HEAD revision returned for {repo}")
        revision = lowered
    return {"state": "available", "head_revision": revision}


def _string_list(value: Any, *, field: str, source_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(f"discovery source {source_id} {field} must be a list")
    if len(value) > _MAX_PLANNING_REFERENCES:
        raise ValidationError(
            f"discovery source {source_id} {field} exceeds {_MAX_PLANNING_REFERENCES} entries"
        )
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(
            f"discovery source {source_id} {field} must contain non-empty strings"
        )
    return [item.strip() for item in value]


def _load_discovery_registry(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expanded = path.expanduser()
    if not expanded.exists():
        return (
            {"path": str(expanded), "status": "missing", "schema_version": None},
            [],
        )
    try:
        raw = json.loads(expanded.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read discovery registry {expanded}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError("discovery registry must be a JSON object")
    repositories = raw.get("repositories")
    if not isinstance(repositories, list):
        raise ValidationError("discovery registry repositories must be a list")
    if len(repositories) > _MAX_DISCOVERY_REPOSITORIES:
        raise ValidationError(
            f"discovery registry exceeds {_MAX_DISCOVERY_REPOSITORIES} repositories"
        )

    normalized: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()
    for index, item in enumerate(repositories):
        if not isinstance(item, dict):
            raise ValidationError(f"discovery repository entry {index} must be an object")
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValidationError(f"discovery repository entry {index} requires source_id")
        source_id = source_id.strip()
        if source_id in seen_source_ids:
            raise ValidationError(f"duplicate discovery source_id {source_id}")
        seen_source_ids.add(source_id)
        root = item.get("root")
        remote = item.get("remote")
        authority = item.get("authority")
        if root is not None and (not isinstance(root, str) or not root.strip()):
            raise ValidationError(f"discovery source {source_id} root must be a non-empty string")
        if remote is not None and not isinstance(remote, str):
            raise ValidationError(f"discovery source {source_id} remote must be a string or null")
        if authority is not None and not isinstance(authority, str):
            raise ValidationError(
                f"discovery source {source_id} authority must be a string or null"
            )
        normalized.append(
            {
                "source_id": source_id,
                "root": root.strip() if isinstance(root, str) else None,
                "remote": remote.strip() if isinstance(remote, str) and remote.strip() else None,
                "authority": authority,
                "planning_files": _string_list(
                    item.get("planning_files"), field="planning_files", source_id=source_id
                ),
                "vault_paths": _string_list(
                    item.get("vault_paths"), field="vault_paths", source_id=source_id
                ),
            }
        )

    schema_version = raw.get("schema_version")
    if schema_version is not None and not isinstance(schema_version, int):
        raise ValidationError("discovery registry schema_version must be an integer or null")
    return (
        {"path": str(expanded), "status": "loaded", "schema_version": schema_version},
        normalized,
    )


def _match_source(resource: Resource, sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    resource_root = _normalized_path(resource.path) if resource.path else None
    resource_slug = _normalized_github_slug(resource.github_slug)
    matches: list[tuple[dict[str, Any], bool, bool]] = []

    for source in sources:
        source_root = _normalized_path(source["root"]) if source["root"] else None
        source_slug = _normalized_github_slug(source["remote"])
        root_match = bool(resource_root and source_root and resource_root == source_root)
        remote_match = bool(resource_slug and source_slug and resource_slug == source_slug)
        if root_match and resource_slug and source_slug and resource_slug != source_slug:
            raise ValidationError(
                f"discovery source {source['source_id']} contradicts canonical remote "
                f"for {resource.id}"
            )
        if root_match or remote_match:
            matches.append((source, root_match, remote_match))

    if len(matches) > 1:
        source_ids = ", ".join(sorted(match[0]["source_id"] for match in matches))
        raise ValidationError(f"ambiguous discovery mapping for {resource.id}: {source_ids}")
    if not matches:
        return None

    source, root_match, remote_match = matches[0]
    match_basis = (
        "root+remote" if root_match and remote_match else "root" if root_match else "remote"
    )
    return {
        "source_id": source["source_id"],
        "root": source["root"],
        "remote": source["remote"],
        "authority": source["authority"],
        "match_basis": match_basis,
        "planning_files": source["planning_files"],
        "vault_paths": source["vault_paths"],
    }


def _evidence(match: dict[str, Any] | None) -> list[dict[str, Any]]:
    if match is None:
        return []
    source_root = Path(match["root"]).expanduser() if match["root"] else None
    evidence: dict[tuple[str, str], dict[str, Any]] = {}

    for declared in match["planning_files"]:
        location_path = Path(declared).expanduser()
        if not location_path.is_absolute() and source_root is not None:
            location_path = source_root / location_path
        location = _normalized_path(location_path)
        key = ("repository-planning-file", location)
        evidence[key] = {
            "evidence_source": "discovery-source-registry",
            "source_id": match["source_id"],
            "kind": key[0],
            "location": location,
            "authority": match["authority"],
            "present": Path(location).exists(),
        }

    for declared in match["vault_paths"]:
        location = _normalized_path(declared)
        key = ("vault-planning-path", location)
        evidence[key] = {
            "evidence_source": "discovery-source-registry",
            "source_id": match["source_id"],
            "kind": key[0],
            "location": location,
            "authority": match["authority"],
            "present": Path(location).exists(),
        }

    return [evidence[key] for key in sorted(evidence)]


def _source_projection(match: dict[str, Any] | None) -> dict[str, Any] | None:
    if match is None:
        return None
    return {
        "source_id": match["source_id"],
        "root": match["root"],
        "remote": match["remote"],
        "authority": match["authority"],
        "match_basis": match["match_basis"],
    }


def scan_repository_registry(
    registry: Any,
    *,
    discovery_registry_path: Path = DEFAULT_DISCOVERY_REGISTRY,
    resource_id: str | None = None,
) -> dict[str, Any]:
    """Project canonical repository identity plus planning evidence without mutation."""

    resources = sorted(
        (resource for resource in registry.resources.values() if resource.type == "git-repository"),
        key=lambda resource: resource.id,
    )
    if len(resources) > _MAX_REPOSITORIES:
        raise ValidationError(f"repository registry exceeds {_MAX_REPOSITORIES} entries")
    if resource_id is not None:
        selected = registry.resources.get(resource_id)
        if selected is None or selected.type != "git-repository":
            raise ValidationError(f"unknown git-repository resource {resource_id}")
        resources = [selected]

    discovery, sources = _load_discovery_registry(discovery_registry_path)
    repositories: list[dict[str, Any]] = []
    for resource in resources:
        match = _match_source(resource, sources)
        repositories.append(
            {
                "resource_id": resource.id,
                "path": resource.path,
                "github_slug": resource.github_slug,
                "grabowski_key": resource.grabowski_key,
                "criticality": resource.criticality,
                "status": _inspect_repository(resource.path),
                "source_match": _source_projection(match),
                "planning_evidence": _evidence(match),
            }
        )

    return {
        "schema_version": 1,
        "kind": "bureau_repo_scan",
        "mode": "dry-run",
        "read_only": True,
        "identity_authority": "bureau-registry",
        "planning_evidence_role": "evidence-only",
        "discovery_registry": discovery,
        "repositories": repositories,
        "does_not_establish": [
            "repository_mutation_authority",
            "task_truth_from_evidence_volume",
            "task_priority",
            "task_state",
        ],
    }
