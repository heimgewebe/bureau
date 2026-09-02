from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import fetch_orchestration
from bureau.approval import ApprovalRequired, explicit_operator_approval
from bureau.legacy import Registry, Resource, StateError

TASK_ID = "BUR-TEST-001-T006"
_CLEAR_RUNTIME = {
    "execution_blocked": False,
    "reason_code": "runtime-clear",
    "summary": "runtime clear",
}
_BLOCKED_RUNTIME = {
    "execution_blocked": True,
    "reason_code": "runtime-drift",
    "summary": "runtime drift blocks execution",
}


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(
        repo,
        "-c",
        "user.name=Bureau Test",
        "-c",
        "user.email=bureau@example.invalid",
        "commit",
        "-m",
        content.strip(),
    )
    return _git(repo, "rev-parse", "HEAD")


def _repository_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Registry, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    target = tmp_path / "target"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", remote], check=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", seed], check=True)
    _commit(seed, "state.txt", "one\n")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(remote), target], check=True)
    _commit(seed, "state.txt", "two\n")
    _git(seed, "push", "origin", "main")

    registry = Registry(tmp_path / "bureau")
    registry.resources["repo.test"] = Resource(
        id="repo.test",
        type="git-repository",
        parent=None,
        capacity=None,
        path=str(target),
        github_slug=None,
        grabowski_key=None,
    )
    discovery = tmp_path / "missing-discovery.json"
    return remote, seed, target, registry, discovery


def _plan(
    root: Path,
    registry: Registry,
    discovery: Path,
) -> dict:
    return fetch_orchestration.repo_fetch_plan(
        root,
        registry,
        "repo.test",
        task_id=TASK_ID,
        discovery_registry_path=discovery,
    )


def test_fetch_plan_is_read_only_stable_and_provenance_rich(tmp_path, monkeypatch) -> None:
    _remote, seed, target, registry, discovery = _repository_fixture(tmp_path)
    monkeypatch.setattr(
        fetch_orchestration, "_runtime_gate", lambda *args, **kwargs: _CLEAR_RUNTIME
    )
    before_head = _git(target, "rev-parse", "HEAD")
    before_tracking = _git(target, "rev-parse", "refs/remotes/origin/main")
    before_status = _git(target, "status", "--porcelain=v1")

    first = _plan(tmp_path, registry, discovery)
    second = _plan(tmp_path, registry, discovery)

    assert first == second
    assert first["allowed"] is True
    assert first["read_only"] is True
    assert first["source"]["commit_sha"] == _git(seed, "rev-parse", "HEAD")
    assert first["destination"]["before_commit"] == before_tracking
    assert first["does_not_mutate"] == ["HEAD", "current_branch", "index", "worktree"]
    assert _git(target, "rev-parse", "HEAD") == before_head
    assert _git(target, "rev-parse", "refs/remotes/origin/main") == before_tracking
    assert _git(target, "status", "--porcelain=v1") == before_status


def test_runtime_blocker_is_structured_and_prevents_plan_apply(tmp_path, monkeypatch) -> None:
    _remote, _seed, _target, registry, discovery = _repository_fixture(tmp_path)
    monkeypatch.setattr(
        fetch_orchestration, "_runtime_gate", lambda *args, **kwargs: _BLOCKED_RUNTIME
    )

    plan = _plan(tmp_path, registry, discovery)

    assert plan["allowed"] is False
    conflict = plan["conflicts"][0]
    assert conflict["code"] == "runtime-drift-blocked"
    assert conflict["repo"]["resource_id"] == "repo.test"
    assert conflict["branch"] == "main"
    assert conflict["source"]["remote"] == "origin"
    assert conflict["required_human_decision"]
    with pytest.raises(StateError, match="blocked by precondition conflicts"):
        fetch_orchestration.apply_repo_fetch_plan(
            tmp_path,
            registry,
            "repo.test",
            expected_plan_sha256=plan["plan_sha256"],
            approval=None,
            task_id=TASK_ID,
            discovery_registry_path=discovery,
        )


def test_fetch_requires_plan_bound_operator_approval_before_mutation(tmp_path, monkeypatch) -> None:
    _remote, _seed, target, registry, discovery = _repository_fixture(tmp_path)
    monkeypatch.setattr(
        fetch_orchestration, "_runtime_gate", lambda *args, **kwargs: _CLEAR_RUNTIME
    )
    plan = _plan(tmp_path, registry, discovery)
    before_tracking = _git(target, "rev-parse", "refs/remotes/origin/main")

    with pytest.raises(ApprovalRequired):
        fetch_orchestration.apply_repo_fetch_plan(
            tmp_path,
            registry,
            "repo.test",
            expected_plan_sha256=plan["plan_sha256"],
            approval=None,
            task_id=TASK_ID,
            discovery_registry_path=discovery,
        )
    assert _git(target, "rev-parse", "refs/remotes/origin/main") == before_tracking
    assert _git(target, "for-each-ref", "--format=%(refname)", "refs/bureau/fetch") == ""


def test_fetch_apply_updates_only_remote_tracking_ref(tmp_path, monkeypatch) -> None:
    _remote, seed, target, registry, discovery = _repository_fixture(tmp_path)
    monkeypatch.setattr(
        fetch_orchestration, "_runtime_gate", lambda *args, **kwargs: _CLEAR_RUNTIME
    )
    plan = _plan(tmp_path, registry, discovery)
    before_head = _git(target, "rev-parse", "HEAD")
    before_branch = _git(target, "symbolic-ref", "--short", "HEAD")
    before_status = _git(target, "status", "--porcelain=v1")
    approval = explicit_operator_approval(
        source="test",
        approved=True,
        reviewer="operator",
        reference=plan["plan_sha256"],
        task_id=TASK_ID,
    )

    receipt = fetch_orchestration.apply_repo_fetch_plan(
        tmp_path,
        registry,
        "repo.test",
        expected_plan_sha256=plan["plan_sha256"],
        approval=approval,
        task_id=TASK_ID,
        discovery_registry_path=discovery,
    )

    expected = _git(seed, "rev-parse", "HEAD")
    assert receipt["status"] == "applied"
    assert receipt["source"]["commit_sha"] == expected
    assert receipt["destination"]["after_commit"] == expected
    assert receipt["worktree_mutated"] is False
    assert receipt["receipt_sha256"]
    assert _git(target, "rev-parse", "refs/remotes/origin/main") == expected
    assert _git(target, "rev-parse", "HEAD") == before_head
    assert _git(target, "symbolic-ref", "--short", "HEAD") == before_branch
    assert _git(target, "status", "--porcelain=v1") == before_status
    assert _git(target, "for-each-ref", "--format=%(refname)", "refs/bureau/fetch") == ""


def test_fetch_refuses_non_fast_forward_and_cleans_temp_ref(tmp_path, monkeypatch) -> None:
    remote, seed, target, registry, discovery = _repository_fixture(tmp_path)
    monkeypatch.setattr(
        fetch_orchestration, "_runtime_gate", lambda *args, **kwargs: _CLEAR_RUNTIME
    )
    # Advance the local tracking ref to the current remote commit first.
    _git(target, "fetch", "origin", "main")
    before_tracking = _git(target, "rev-parse", "refs/remotes/origin/main")
    # Rewrite remote main from the original clone commit so the new source is divergent.
    clone_head = _git(target, "rev-parse", "HEAD")
    _git(seed, "checkout", "--detach", clone_head)
    rewritten = _commit(seed, "rewrite.txt", "rewrite\n")
    _git(seed, "push", "--force", "origin", f"{rewritten}:main")
    assert _git(remote, "rev-parse", "refs/heads/main") == rewritten

    plan = _plan(tmp_path, registry, discovery)
    approval = explicit_operator_approval(
        source="test",
        approved=True,
        reference=plan["plan_sha256"],
        task_id=TASK_ID,
    )
    receipt = fetch_orchestration.apply_repo_fetch_plan(
        tmp_path,
        registry,
        "repo.test",
        expected_plan_sha256=plan["plan_sha256"],
        approval=approval,
        task_id=TASK_ID,
        discovery_registry_path=discovery,
    )

    assert receipt["status"] == "conflict"
    assert receipt["conflict"]["code"] == "non-fast-forward"
    assert receipt["conflict"]["required_human_decision"]
    assert _git(target, "rev-parse", "refs/remotes/origin/main") == before_tracking
    assert _git(target, "for-each-ref", "--format=%(refname)", "refs/bureau/fetch") == ""


def test_fetch_rejects_drifted_plan_before_mutation(tmp_path, monkeypatch) -> None:
    _remote, seed, target, registry, discovery = _repository_fixture(tmp_path)
    monkeypatch.setattr(
        fetch_orchestration, "_runtime_gate", lambda *args, **kwargs: _CLEAR_RUNTIME
    )
    plan = _plan(tmp_path, registry, discovery)
    before_tracking = _git(target, "rev-parse", "refs/remotes/origin/main")
    _commit(seed, "third.txt", "three\n")
    _git(seed, "push", "origin", "HEAD:main")
    approval = explicit_operator_approval(
        source="test",
        approved=True,
        reference=plan["plan_sha256"],
        task_id=TASK_ID,
    )

    with pytest.raises(StateError, match="fetch plan drifted"):
        fetch_orchestration.apply_repo_fetch_plan(
            tmp_path,
            registry,
            "repo.test",
            expected_plan_sha256=plan["plan_sha256"],
            approval=approval,
            task_id=TASK_ID,
            discovery_registry_path=discovery,
        )
    assert _git(target, "rev-parse", "refs/remotes/origin/main") == before_tracking



def test_repo_fetch_cli_is_read_only_until_exact_plan_is_applied() -> None:
    preview = bureau_cli.parser().parse_args(
        ["repo-fetch", "--repo", "repo.test", "--task-id", TASK_ID]
    )
    apply = bureau_cli.parser().parse_args(
        [
            "repo-fetch",
            "--repo",
            "repo.test",
            "--task-id",
            TASK_ID,
            "--apply-plan",
            "a" * 64,
            "--approve",
        ]
    )

    assert bureau_cli._command_mutates(preview) is False
    assert bureau_cli._command_effect_scope(preview) == "read_only"
    assert bureau_cli._command_mutates(apply) is True


def test_source_import_cli_is_read_only_until_exact_plan_is_applied() -> None:
    preview = bureau_cli.parser().parse_args(
        ["source-import", "weltgewebe", "--repo", "/tmp/source", "--task-id", TASK_ID]
    )
    apply = bureau_cli.parser().parse_args(
        [
            "source-import",
            "weltgewebe",
            "--repo",
            "/tmp/source",
            "--task-id",
            TASK_ID,
            "--apply-plan",
            "b" * 64,
            "--reviewed-receipt",
            "--reviewer",
            "reviewer",
        ]
    )

    assert bureau_cli._command_mutates(preview) is False
    assert bureau_cli._command_effect_scope(preview) == "read_only"
    assert bureau_cli._command_mutates(apply) is True
