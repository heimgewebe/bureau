from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bureau.github_observer import BINDING_AMBIGUOUS, BINDING_UNMATCHED, observe_pull_requests


def pull_request(
    number: int = 7,
    *,
    title: str = "Example",
    body: str = "",
    branch: str = "feat/example",
    labels: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/heimgewebe/bureau/pull/{number}",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": branch,
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "statusCheckRollup": [],
        "updatedAt": "2026-07-08T11:00:00Z",
        "labels": labels or [],
    }


def task(
    task_id: str,
    *,
    resource: str = "repo.bureau",
    state: str = "ready",
    raw: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        state=state,
        claims=(SimpleNamespace(resource=resource),),
        raw=raw if raw is not None else {"id": task_id, "state": state},
    )


def registry(*tasks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(tasks={item.id: item for item in tasks})


def observe(
    pull_requests: list[dict[str, object]], tmp_path: Path, *, registry_value=None
) -> dict[str, object]:
    return observe_pull_requests(
        tmp_path,
        repository="heimgewebe/bureau",
        pull_requests=pull_requests,
        state_db=tmp_path / "missing.sqlite3",
        registry=registry_value,
    )


def finding_codes(result: dict[str, object]) -> set[str]:
    return {str(item["code"]) for item in result["hard_findings"]}


def test_open_pr_without_task_binding_is_merge_blocking(tmp_path: Path) -> None:
    result = observe([pull_request(body="No Bureau task marker.")], tmp_path)

    assert result["healthy"] is True
    assert result["binding_healthy"] is False
    assert result["pull_requests"][0]["binding"] == BINDING_UNMATCHED
    assert finding_codes(result) == {"missing-github-task-binding"}


def test_unknown_task_marker_is_merge_blocking(tmp_path: Path) -> None:
    result = observe(
        [pull_request(body="Bureau-Task: BUR-X-T404")],
        tmp_path,
        registry_value=registry(task("BUR-X-T001")),
    )

    assert result["binding_healthy"] is False
    assert finding_codes(result) == {"invalid-github-task-binding"}


def test_bound_task_must_claim_observed_repository(tmp_path: Path) -> None:
    result = observe(
        [pull_request(body="Bureau-Task: BUR-X-T001")],
        tmp_path,
        registry_value=registry(task("BUR-X-T001", resource="repo.heimlern")),
    )

    assert result["binding_healthy"] is False
    assert finding_codes(result) == {"wrong-repository-github-task-binding"}


def test_terminal_task_without_follow_up_is_merge_blocking(tmp_path: Path) -> None:
    result = observe(
        [pull_request(body="Bureau-Task: BUR-X-T001")],
        tmp_path,
        registry_value=registry(task("BUR-X-T001", state="verified")),
    )

    assert result["binding_healthy"] is False
    assert finding_codes(result) == {"terminal-github-task-binding"}


def test_terminal_task_with_schema_visible_follow_up_is_allowed(tmp_path: Path) -> None:
    result = observe(
        [pull_request(body="Bureau-Task: BUR-X-T001")],
        tmp_path,
        registry_value=registry(
            task(
                "BUR-X-T001",
                state="verified",
                raw={
                    "id": "BUR-X-T001",
                    "state": "verified",
                    "metadata": {"follow_up_task": "BUR-X-T002"},
                },
            )
        ),
    )

    assert result["binding_healthy"] is True
    assert result["hard_findings"] == []


def test_schema_visible_exception_suppresses_taskless_binding_blocker(tmp_path: Path) -> None:
    result = observe(
        [
            pull_request(
                body="Bureau-PR-Task-Binding-Exception: documentation-only tracking PR"
            )
        ],
        tmp_path,
        registry_value=registry(task("BUR-X-T001")),
    )

    observation = result["pull_requests"][0]
    assert observation["binding"] == BINDING_UNMATCHED
    assert observation["binding_exception"] == {
        "source": "body",
        "reason": "documentation-only tracking PR",
    }
    assert result["binding_healthy"] is True
    assert result["hard_findings"] == []


def test_schema_visible_exception_suppresses_multi_task_binding_blocker(tmp_path: Path) -> None:
    result = observe(
        [
            pull_request(
                body="Bureau-Tasks: BUR-X-T001, BUR-X-T002\n"
                "Bureau-PR-Task-Binding-Exception: explicit cross-task closeout"
            )
        ],
        tmp_path,
        registry_value=registry(task("BUR-X-T001"), task("BUR-X-T002")),
    )

    observation = result["pull_requests"][0]
    assert observation["binding"] == BINDING_AMBIGUOUS
    assert observation["binding_exception"]["reason"] == "explicit cross-task closeout"
    assert result["binding_healthy"] is True
    assert result["hard_findings"] == []
