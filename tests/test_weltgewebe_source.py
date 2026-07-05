from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import weltgewebe_source
from bureau.core import Registry, ValidationError
from bureau.weltgewebe_source import source_check, source_sync

SOURCE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["schema_version", "curation", "source_files", "tasks"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string"},
        "generated_at": {"type": ["string", "null"]},
        "curation": {"type": "string"},
        "source_files": {"type": "array", "items": {"type": "string"}},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "title",
                    "area",
                    "status",
                    "priority",
                    "effort",
                    "risk",
                    "owner",
                    "evidence",
                    "missing_evidence",
                    "acceptance",
                    "links",
                    "updated_at",
                ],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Z]+(-[A-Z]+)*-[0-9]{3}$"},
                    "title": {"type": "string", "minLength": 1},
                    "area": {
                        "enum": [
                            "docs", "ci", "api", "web", "infra",
                            "release", "auth", "map", "governance",
                        ]
                    },
                    "status": {
                        "enum": [
                            "open", "partial", "done", "blocked",
                            "obsolete", "contradicted",
                        ]
                    },
                    "priority": {"enum": ["high", "medium", "low"]},
                    "effort": {"enum": ["XS", "S", "M", "L", "XL"]},
                    "risk": {"enum": ["low", "medium", "high"]},
                    "owner": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "missing_evidence": {"type": "array", "items": {"type": "string"}},
                    "acceptance": {"type": "array", "items": {"type": "string"}},
                    "links": {
                        "type": "object",
                        "required": ["issues", "prs", "docs"],
                        "additionalProperties": False,
                        "properties": {
                            "issues": {"type": "array", "items": {"type": "string"}},
                            "prs": {"type": "array", "items": {"type": "string"}},
                            "docs": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "updated_at": {"type": "string"},
                },
            },
        },
    },
}


def task(identifier: str, status: str = "open", title: str | None = None) -> dict:
    return {
        "id": identifier,
        "title": title or identifier,
        "area": "docs",
        "status": status,
        "priority": "medium",
        "effort": "S",
        "risk": "low",
        "owner": "unknown",
        "evidence": [],
        "missing_evidence": ["proof"],
        "acceptance": ["criterion"],
        "links": {"issues": [], "prs": [], "docs": []},
        "updated_at": "2026-06-28",
    }


def write_source(repository: Path, tasks: list[dict]) -> None:
    docs = repository / "docs/tasks"
    docs.mkdir(parents=True, exist_ok=True)
    index = {
        "schema_version": "1.0.0",
        "generated_at": None,
        "curation": "manual",
        "source_files": ["docs/tasks/board.md"],
        "tasks": tasks,
    }
    (docs / "schema.json").write_text(json.dumps(SOURCE_SCHEMA), encoding="utf-8")
    (docs / "index.json").write_text(json.dumps(index), encoding="utf-8")


def commit_source(repository: Path, message: str = "source") -> str:
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Bureau Test",
            "-c",
            "user.email=bureau@example.invalid",
            "commit",
            "-m",
            message,
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "weltgewebe"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", repository], check=True)
    write_source(
        repository,
        [task("TASK-ONE-001", "open"), task("TASK-TWO-002", "done")],
    )
    commit_source(repository)
    return repository


def test_source_check_is_commit_bound_and_read_only(
    registry_factory, source_repo, tmp_path, monkeypatch, capsys
):
    root = registry_factory(1)
    state = tmp_path / "must-not-exist"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--json",
            "source-check",
            "weltgewebe",
            "--repo",
            str(source_repo),
            "--ref",
            "HEAD",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["commit_sha"] == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert report["task_count"] == 2
    assert report["status_counts"]["open"] == 1
    assert report["status_counts"]["done"] == 1
    assert not state.exists()
    assert not (root / "registry/sources/weltgewebe.json").exists()


def test_source_sync_preview_is_read_only(
    registry_factory, source_repo, tmp_path, monkeypatch, capsys
):
    root = registry_factory(1)
    state = tmp_path / "must-not-exist-preview"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--json",
            "source-sync",
            "weltgewebe",
            "--repo",
            str(source_repo),
            "--ref",
            "HEAD",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["changed"] is True
    assert report["applied"] is False
    assert not state.exists()
    assert not (root / "registry/sources/weltgewebe.json").exists()


def test_source_sync_apply_is_valid_and_idempotent(registry_factory, source_repo):
    root = registry_factory(1)
    first = source_sync(root, source_repo, "HEAD", apply=True)
    target = root / "registry/sources/weltgewebe.json"
    assert first["applied"] is True
    assert target.is_file()
    Registry.load(root)
    before = target.stat().st_mtime_ns
    second = source_sync(root, source_repo, "HEAD", apply=True)
    alias = source_sync(root, source_repo, first["commit_sha"], apply=True)
    assert second["applied"] is False
    assert second["changed"] is False
    assert not alias["changed"]
    assert target.stat().st_mtime_ns == before
    (source_repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    commit_source(source_repo, "unrelated")
    third = source_sync(root, source_repo, "HEAD", apply=True)
    assert not third["changed"]
    assert not third["applied"]
    assert target.stat().st_mtime_ns == before
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    assert snapshot["active_task_ids"] == ["TASK-ONE-001"]
    assert snapshot["repository"] == "heimgewebe/weltgewebe"
    assert str(source_repo) not in target.read_text(encoding="utf-8")
    assert snapshot["entries"][0]["source_task"]["id"] == "TASK-ONE-001"
    assert "bureau_task_materialization" in snapshot["does_not_establish"]


def test_source_change_reports_only_changed_entry(registry_factory, source_repo):
    root = registry_factory(1)
    source_sync(root, source_repo, "HEAD", apply=True)
    write_source(
        source_repo,
        [task("TASK-ONE-001", "open", "changed"), task("TASK-TWO-002", "done")],
    )
    commit_source(source_repo, "change")
    preview = source_sync(root, source_repo, "HEAD")
    assert preview["changed"] is True
    assert preview["changes"]["added"]["count"] == 0
    assert preview["changes"]["changed"]["ids"] == ["TASK-ONE-001"]
    assert preview["changes"]["removed"]["count"] == 0


def test_preview_is_bounded(registry_factory, tmp_path):
    source = tmp_path / "many"
    source.mkdir()
    subprocess.run(["git", "init", "-q", source], check=True)
    tasks = [task(f"TASK-MANY-{index:03d}") for index in range(1, 61)]
    write_source(source, tasks)
    commit_source(source)
    report = source_sync(registry_factory(1), source, "HEAD")
    assert report["active_tasks"]["count"] == 60
    assert len(report["active_tasks"]["ids"]) == 50
    assert report["active_tasks"]["truncated"] is True
    assert "entries" not in report


def test_ref_injection_and_duplicate_ids_fail_closed(source_repo):
    with pytest.raises(ValidationError, match="invalid Git ref"):
        source_check(source_repo, "--help")
    write_source(source_repo, [task("TASK-DUPE-001"), task("TASK-DUPE-001")])
    commit_source(source_repo, "duplicates")
    with pytest.raises(ValidationError, match="duplicate Weltgewebe task ids"):
        source_check(source_repo, "HEAD")


def test_git_read_ignores_repository_pager_configuration(source_repo, tmp_path):
    marker = tmp_path / "pager-ran"
    subprocess.run(
        ["git", "config", "core.pager", f"sh -c 'touch {marker}'"],
        cwd=source_repo,
        check=True,
    )
    source_check(source_repo, "HEAD")
    assert not marker.exists()


def test_external_schema_reference_is_rejected(source_repo):
    schema_path = source_repo / "docs/tasks/schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["tasks"] = {"$ref": "https://example.invalid/task.json"}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    commit_source(source_repo, "external ref")
    with pytest.raises(ValidationError, match="external references"):
        source_check(source_repo, "HEAD")


def test_git_source_reader_disables_replacement_objects():
    assert weltgewebe_source._git_environment()["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_invalid_source_schema_is_reported_as_validation_error(source_repo):
    schema_path = source_repo / "docs/tasks/schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["tasks"]["type"] = 7
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    commit_source(source_repo, "invalid schema")
    with pytest.raises(ValidationError, match="invalid Weltgewebe source schema"):
        source_check(source_repo, "HEAD")


def test_registry_rejects_source_task_hash_drift(registry_factory, source_repo):
    root = registry_factory(1)
    source_sync(root, source_repo, "HEAD", apply=True)
    target = root / "registry/sources/weltgewebe.json"
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    snapshot["entries"][0]["source_task"]["title"] = "tampered"
    target.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValidationError, match="task hash mismatch"):
        Registry.load(root)


def test_registry_rejects_unknown_source_property(registry_factory, source_repo):
    root = registry_factory(1)
    source_sync(root, source_repo, "HEAD", apply=True)
    target = root / "registry/sources/weltgewebe.json"
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    snapshot["unexpected"] = True
    target.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValidationError, match="unexpected"):
        Registry.load(root)
