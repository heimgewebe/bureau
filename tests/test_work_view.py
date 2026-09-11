from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from bureau import legacy
from bureau.work_view import resource_work_view

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resource(
    resource_id: str,
    *,
    path: str | None = None,
    github_slug: str | None = None,
) -> dict:
    value = {
        "schema_version": 1,
        "id": resource_id,
        "type": "git-repository" if path else "group",
    }
    if resource_id != "repo":
        value["parent"] = "repo"
    if path:
        value["path"] = path
        value["grabowski_key"] = f"repo:{path}"
    if github_slug:
        value["github_slug"] = github_slug
    return value


def _task(
    task_id: str,
    *,
    claims: list[dict] | None = None,
    working_repository: str | None = None,
    grabowski_resources: list[str] | None = None,
    depends_on: list[str] | None = None,
    parent_task: str | None = None,
    title: str | None = None,
    state: str = "planned",
) -> dict:
    execution = {"mode": "manual", "policy": "manual"}
    if working_repository is not None:
        execution["working_repository"] = working_repository
    if grabowski_resources is not None:
        execution["grabowski_resources"] = grabowski_resources
    value = {
        "schema_version": 1,
        "id": task_id,
        "initiative": "TEST-WORK-V1",
        "title": title or task_id,
        "state": state,
        "priority": {"lane": "later", "rank": 100},
        "execution": execution,
        "claims": claims or [],
        "acceptance": [{"id": "visible", "assertion": "Projection stays read-only."}],
    }
    if depends_on:
        value["depends_on"] = depends_on
    if parent_task:
        value["metadata"] = {"parent_task": parent_task}
    return value


def _fixture(root: Path) -> None:
    shutil.copytree(REPO_ROOT / "schemas", root / "schemas")
    _write_json(root / "registry/resources/repo.json", _resource("repo"))
    _write_json(
        root / "registry/resources/commonthing.json",
        _resource(
            "repo.commonthing",
            path="/home/alex/repos/commonthing",
            github_slug="heimgewebe/commonthing",
        ),
    )
    _write_json(
        root / "registry/resources/weltgewebe.json",
        _resource(
            "repo.weltgewebe",
            path="/home/alex/repos/weltgewebe",
            github_slug="heimgewebe/weltgewebe",
        ),
    )
    _write_json(
        root / "registry/resources/other.json",
        _resource("repo.other", path="/home/alex/repos/other", github_slug="heimgewebe/other"),
    )
    _write_json(
        root / "registry/initiatives/TEST-WORK-V1.json",
        {
            "schema_version": 1,
            "id": "TEST-WORK-V1",
            "title": "Work view fixture",
            "state": "active",
            "commitment": "now",
            "goal": "Exercise resource work projection.",
            "completion": ["Projection remains read-only."],
            "parallelism": {"max_active_tasks": 2},
        },
    )

    _write_json(
        root / "registry/tasks/TEST-WORK-V1-T001.json",
        _task(
            "TEST-WORK-V1-T001",
            claims=[
                {
                    "resource": "repo.commonthing",
                    "mode": "write",
                    "isolation": "worktree",
                }
            ],
        ),
    )
    _write_json(
        root / "registry/tasks/TEST-WORK-V1-T002.json",
        _task(
            "TEST-WORK-V1-T002",
            working_repository="/home/alex/repos/commonthing",
        ),
    )
    _write_json(
        root / "registry/tasks/TEST-WORK-V1-T003.json",
        _task(
            "TEST-WORK-V1-T003",
            claims=[{"resource": "repo.other", "mode": "read"}],
            grabowski_resources=[
                "repo:/home/alex/repos/commonthing:branch:feat/example"
            ],
        ),
    )
    _write_json(
        root / "registry/tasks/TEST-WORK-V1-T004.json",
        _task(
            "TEST-WORK-V1-T004",
            claims=[{"resource": "repo.other", "mode": "read"}],
            depends_on=["TEST-WORK-V1-T001"],
        ),
    )
    _write_json(
        root / "registry/tasks/TEST-WORK-V1-T005.json",
        _task(
            "TEST-WORK-V1-T005",
            claims=[{"resource": "repo.weltgewebe", "mode": "read"}],
            parent_task="TEST-WORK-V1-T002",
        ),
    )
    _write_json(
        root / "registry/tasks/TEST-WORK-V1-T006.json",
        _task(
            "TEST-WORK-V1-T006",
            claims=[{"resource": "repo.other", "mode": "read"}],
            title="Commonthing only appears in prose",
        ),
    )
    _write_json(
        root / "registry/tasks/TEST-WORK-V1-T007.json",
        _task(
            "TEST-WORK-V1-T007",
            claims=[{"resource": "repo.other", "mode": "read"}],
            depends_on=["TEST-WORK-V1-T004"],
        ),
    )
    _write_json(
        root / "registry/tasks/TEST-WORK-V1-T008.json",
        _task(
            "TEST-WORK-V1-T008",
            claims=[{"resource": "repo.weltgewebe", "mode": "read"}],
            working_repository="/home/alex/repos/weltgewebe",
            state="ready",
        ),
    )


def _candidate(
    event_id: int,
    candidate_id: str,
    *,
    repo: str | None = None,
    task_id: str | None = None,
    title: str = "candidate",
) -> dict:
    return {
        "event_id": event_id,
        "record": {
            "kind": "candidate_task",
            "candidate_id": candidate_id,
            "title": title,
            "status": "observed",
            "repo": repo,
            "task_id": task_id,
            "promotion_required": True,
        },
    }


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _task_ids(view: dict, section: str) -> list[str]:
    return [item["task_id"] for item in view["sections"][section]]


def _candidate_ids(view: dict, section: str) -> list[str]:
    return [item["candidate_id"] for item in view["candidates"][section]]


def test_resource_work_view_classifies_direct_related_legacy_and_out(tmp_path: Path) -> None:
    _fixture(tmp_path)
    state_db = tmp_path / "state/bureau.sqlite3"
    candidates = [
        _candidate(4, "candidate-prose", repo="repo.other", title="Commonthing in prose"),
        _candidate(
            3,
            "candidate-legacy",
            repo="repo.weltgewebe",
            task_id="TEST-WORK-V1-T005",
        ),
        _candidate(2, "candidate-related", task_id="TEST-WORK-V1-T004"),
        _candidate(1, "candidate-direct", repo="repo.commonthing"),
    ]

    before = _tree_hashes(tmp_path)
    view = resource_work_view(
        tmp_path,
        "repo.commonthing",
        state_db=state_db,
        legacy_resource_ids=("repo.weltgewebe",),
        candidate_records_override=candidates,
        now="2026-09-11T12:00:00Z",
    )
    after = _tree_hashes(tmp_path)

    assert before == after
    assert view["read_only"] is True
    assert view["resource"]["id"] == "repo.commonthing"
    assert _task_ids(view, "direct") == [
        "TEST-WORK-V1-T001",
        "TEST-WORK-V1-T002",
        "TEST-WORK-V1-T003",
    ]
    assert _task_ids(view, "related") == ["TEST-WORK-V1-T004"]
    assert _task_ids(view, "legacy") == ["TEST-WORK-V1-T005"]
    assert view["counts"]["tasks"] == {
        "direct": 3,
        "related": 1,
        "legacy": 1,
        "out": 3,
    }
    assert view["counts"]["migration_gaps"] == 1
    assert view["diagnostics"]["migration_gaps"] == [
        {
            "task_id": "TEST-WORK-V1-T008",
            "title": "TEST-WORK-V1-T008",
            "initiative": "TEST-WORK-V1",
            "task_spec_state": "ready",
            "effective_state": "ready",
            "diagnostic": "declared_legacy_resource_without_explicit_target_relation",
            "membership": None,
            "basis": [
                {
                    "kind": "declared_legacy_resource",
                    "resource": "repo.weltgewebe",
                    "diagnostic_only": True,
                }
            ],
            "does_not_establish": [
                "target_resource_membership",
                "automatic_task_migration",
                "automatic_resource_supersession",
            ],
        }
    ]
    assert "TEST-WORK-V1-T006" not in {
        item["task_id"]
        for section in view["sections"].values()
        for item in section
    }
    assert "TEST-WORK-V1-T007" not in {
        item["task_id"]
        for section in view["sections"].values()
        for item in section
    }
    assert "TEST-WORK-V1-T008" not in {
        item["task_id"]
        for section in view["sections"].values()
        for item in section
    }

    assert _candidate_ids(view, "direct") == ["candidate-direct"]
    assert _candidate_ids(view, "related") == ["candidate-related"]
    assert _candidate_ids(view, "legacy") == ["candidate-legacy"]
    assert all(
        item["authority"] == "candidate_only"
        for section in view["candidates"].values()
        for item in section
    )

    t001 = view["sections"]["direct"][0]
    assert t001["basis"][0]["kind"] == "claim"
    assert t001["basis"][0]["resource"] == "repo.commonthing"
    t004 = view["sections"]["related"][0]
    assert t004["basis"] == [
        {
            "kind": "depends_on",
            "source_task_id": "TEST-WORK-V1-T004",
            "target_task_id": "TEST-WORK-V1-T001",
            "distance": 1,
        }
    ]
    assert view["membership_policy"]["transitive_expansion"] is False
    assert view["membership_policy"]["title_or_id_heuristics"] is False


def test_resource_work_view_is_stable_for_same_snapshot_and_time(tmp_path: Path) -> None:
    _fixture(tmp_path)
    kwargs = {
        "state_db": tmp_path / "state/bureau.sqlite3",
        "legacy_resource_ids": ("repo.weltgewebe",),
        "candidate_records_override": (),
        "now": "2026-09-11T12:00:00Z",
    }

    first = resource_work_view(tmp_path, "repo.commonthing", **kwargs)
    second = resource_work_view(tmp_path, "repo.commonthing", **kwargs)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_legacy_requires_both_explicit_relation_and_declared_legacy_resource(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    view = resource_work_view(
        tmp_path,
        "repo.commonthing",
        state_db=tmp_path / "state/bureau.sqlite3",
        candidate_records_override=(),
        now="2026-09-11T12:00:00Z",
    )

    assert _task_ids(view, "legacy") == []
    assert _task_ids(view, "related") == [
        "TEST-WORK-V1-T004",
        "TEST-WORK-V1-T005",
    ]
    assert view["diagnostics"]["migration_gaps"] == []
    assert view["counts"]["migration_gaps"] == 0


def test_unknown_resource_fails_without_creating_state(tmp_path: Path) -> None:
    _fixture(tmp_path)
    before = _tree_hashes(tmp_path)

    with pytest.raises(legacy.ValidationError, match="unknown work-view resource"):
        resource_work_view(
            tmp_path,
            "repo.missing",
            state_db=tmp_path / "state/bureau.sqlite3",
            candidate_records_override=(),
            now="2026-09-11T12:00:00Z",
        )

    assert _tree_hashes(tmp_path) == before
