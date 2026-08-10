from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from bureau.cli import _READ_ONLY_COMMANDS, parser
from bureau.legacy import Resource, ValidationError
from bureau.repo_scan import scan_repository_registry


def _resource(
    path: Path,
    *,
    resource_id: str = "repo.test",
    github_slug: str = "heimgewebe/test",
) -> Resource:
    return Resource(
        id=resource_id,
        type="git-repository",
        parent=None,
        capacity=None,
        path=str(path),
        github_slug=github_slug,
        grabowski_key=f"repo:{resource_id}",
        criticality="standard",
    )


def _registry(*resources: Resource) -> SimpleNamespace:
    return SimpleNamespace(resources={resource.id: resource for resource in resources})


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(
        path,
        "-c",
        "user.name=Bureau Test",
        "-c",
        "user.email=bureau@example.invalid",
        "commit",
        "-q",
        "-m",
        "initial",
    )


def _write_discovery(path: Path, repositories: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "repositories": repositories}),
        encoding="utf-8",
    )


def _git_files_snapshot(repo: Path) -> dict[str, tuple[int, int, int, str]]:
    result: dict[str, tuple[int, int, int, str]] = {}
    for path in sorted((repo / ".git").rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        metadata = path.stat()
        result[str(path.relative_to(repo))] = (
            stat.S_IMODE(metadata.st_mode),
            metadata.st_size,
            metadata.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


def _worktree_snapshot(repo: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(repo.rglob("*")):
        if ".git" in path.parts or not path.is_file() or path.is_symlink():
            continue
        metadata = path.stat()
        result[str(path.relative_to(repo))] = (
            metadata.st_size,
            metadata.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


def test_projection_uses_canonical_identity_and_evidence_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    legacy_root = tmp_path / "legacy-root"
    (legacy_root / "docs").mkdir(parents=True)
    (legacy_root / "docs" / "plan.md").write_text("plan\n", encoding="utf-8")
    vault = tmp_path / "vault" / "test"
    vault.mkdir(parents=True)
    discovery = tmp_path / "sources.json"
    _write_discovery(
        discovery,
        [
            {
                "source_id": "repo:test",
                "root": str(legacy_root),
                "remote": "https://github.com/heimgewebe/test.git",
                "authority": "repository",
                "planning_files": ["docs/plan.md", "docs/plan.md"],
                "vault_paths": [str(vault), str(vault)],
            }
        ],
    )

    result = scan_repository_registry(_registry(_resource(repo)), discovery_registry_path=discovery)
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "repo-scan.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(schema).validate(result)

    assert result["mode"] == "dry-run"
    assert result["read_only"] is True
    assert result["identity_authority"] == "bureau-registry"
    assert result["planning_evidence_role"] == "evidence-only"
    item = result["repositories"][0]
    assert item["resource_id"] == "repo.test"
    assert item["path"] == str(repo)
    assert item["github_slug"] == "heimgewebe/test"
    assert item["status"]["state"] == "available"
    assert item["status"]["head_revision"] == _git(repo, "rev-parse", "HEAD")
    assert item["source_match"]["source_id"] == "repo:test"
    assert item["source_match"]["match_basis"] == "remote"
    assert [(e["kind"], e["present"]) for e in item["planning_evidence"]] == [
        ("repository-planning-file", True),
        ("vault-planning-path", True),
    ]
    assert "task_truth_from_evidence_volume" in result["does_not_establish"]


def test_statuses_are_bounded_for_missing_and_non_git_paths(tmp_path: Path) -> None:
    missing = _resource(tmp_path / "missing", resource_id="repo.missing", github_slug="x/missing")
    not_git_path = tmp_path / "not-git"
    not_git_path.mkdir()
    not_git = _resource(not_git_path, resource_id="repo.not-git", github_slug="x/not-git")

    result = scan_repository_registry(
        _registry(not_git, missing), discovery_registry_path=tmp_path / "absent.json"
    )
    assert [item["resource_id"] for item in result["repositories"]] == [
        "repo.missing",
        "repo.not-git",
    ]
    assert [item["status"]["state"] for item in result["repositories"]] == [
        "missing",
        "not-git-repository",
    ]


def test_discovery_mapping_is_fail_closed_when_ambiguous(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    discovery = tmp_path / "sources.json"
    _write_discovery(
        discovery,
        [
            {"source_id": "a", "root": str(tmp_path / "a"), "remote": "heimgewebe/test"},
            {"source_id": "b", "root": str(tmp_path / "b"), "remote": "heimgewebe/test"},
        ],
    )
    with pytest.raises(ValidationError, match="ambiguous discovery mapping"):
        scan_repository_registry(_registry(_resource(repo)), discovery_registry_path=discovery)


def test_root_match_with_conflicting_remote_is_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    discovery = tmp_path / "sources.json"
    _write_discovery(
        discovery,
        [{"source_id": "bad", "root": str(repo), "remote": "heimgewebe/other"}],
    )
    with pytest.raises(ValidationError, match="contradicts canonical remote"):
        scan_repository_registry(_registry(_resource(repo)), discovery_registry_path=discovery)


def test_unknown_repo_selector_is_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    with pytest.raises(ValidationError, match="unknown git-repository resource"):
        scan_repository_registry(
            _registry(_resource(repo)),
            discovery_registry_path=tmp_path / "absent.json",
            resource_id="repo.unknown",
        )


def test_cli_classifies_repo_scan_as_intrinsically_read_only() -> None:
    assert "repo-scan" in _READ_ONLY_COMMANDS
    parsed = parser().parse_args(
        ["repo-scan", "--repo", "repo.test", "--discovery-registry", "/tmp/sources.json"]
    )
    assert parsed.command == "repo-scan"
    assert parsed.repo == "repo.test"
    assert parsed.dry_run is False
    parsed_explicit = parser().parse_args(["repo-scan", "--dry-run"])
    assert parsed_explicit.dry_run is True


def test_scan_does_not_mutate_git_or_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    before_head = _git(repo, "rev-parse", "HEAD")
    before_refs = _git(repo, "show-ref")
    before_git = _git_files_snapshot(repo)
    before_worktree = _worktree_snapshot(repo)

    result = scan_repository_registry(
        _registry(_resource(repo)), discovery_registry_path=tmp_path / "absent.json"
    )

    assert result["repositories"][0]["status"]["state"] == "available"
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git(repo, "show-ref") == before_refs
    assert _git_files_snapshot(repo) == before_git
    assert _worktree_snapshot(repo) == before_worktree


def test_empty_discovery_remote_means_unknown_not_invalid(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    discovery = tmp_path / "sources.json"
    _write_discovery(
        discovery,
        [{"source_id": "local", "root": str(repo), "remote": ""}],
    )

    result = scan_repository_registry(
        _registry(_resource(repo)), discovery_registry_path=discovery
    )
    source_match = result["repositories"][0]["source_match"]
    assert source_match["source_id"] == "local"
    assert source_match["remote"] is None
    assert source_match["match_basis"] == "root"
