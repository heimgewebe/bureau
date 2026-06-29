from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau.core import Registry, ValidationError
from bureau.weltgewebe_source import source_promote_plan, source_sync

HELPERS = Path(__file__).with_name("test_weltgewebe_source.py")
SPEC = importlib.util.spec_from_file_location("weltgewebe_source_test_helpers", HELPERS)
assert SPEC is not None and SPEC.loader is not None
HELPER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER_MODULE)
commit_source = HELPER_MODULE.commit_source
task = HELPER_MODULE.task
write_source = HELPER_MODULE.write_source


def test_source_promote_plan_is_read_only_candidate(
    registry_factory, tmp_path, monkeypatch, capsys
):
    source = tmp_path / "weltgewebe"
    source.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", source], check=True)
    write_source(source, [task("TASK-ONE-001", "open"), task("TASK-TWO-002", "done")])
    commit_source(source)
    root = registry_factory(1)
    source_sync(root, source, "HEAD", apply=True)
    state = tmp_path / "must-not-exist-promote"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--json",
            "source-promote-plan",
            "weltgewebe",
            "--task-id",
            "TASK-ONE-001",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert not state.exists()
    assert report["bureau_task_id"] == "WG-TASK-ONE-001"
    assert report["projected_state"] == "planned"
    assert report["materialization_allowed"] is True
    assert report["readiness"] == "blocked"
    assert report["manual_decisions_required"]
    assert report["candidate_task"]["execution"]["policy"] == "review-before-effect"
    assert report["candidate_task"]["metadata"]["source"]["source_task_id"] == "TASK-ONE-001"


def test_source_promote_plan_blocks_terminal_source_task(registry_factory, tmp_path):
    source = tmp_path / "weltgewebe"
    source.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", source], check=True)
    write_source(source, [task("TASK-ONE-001", "open"), task("TASK-TWO-002", "done")])
    commit_source(source)
    root = registry_factory(1)
    source_sync(root, source, "HEAD", apply=True)
    registry = Registry.load(root)
    report = source_promote_plan(root, registry, "weltgewebe", "TASK-TWO-002")
    assert report["bureau_task_id"] == "WG-TASK-TWO-002"
    assert report["projected_state"] == "superseded"
    assert report["materialization_allowed"] is False
    assert "source-task-is-not-active" in report["blockers"]


def test_source_promote_plan_rejects_unknown_task(registry_factory, tmp_path):
    source = tmp_path / "weltgewebe"
    source.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", source], check=True)
    write_source(source, [task("TASK-ONE-001", "open")])
    commit_source(source)
    root = registry_factory(1)
    source_sync(root, source, "HEAD", apply=True)
    registry = Registry.load(root)
    with pytest.raises(ValidationError, match="unknown Weltgewebe source task id"):
        source_promote_plan(root, registry, "weltgewebe", "TASK-NOPE-999")
