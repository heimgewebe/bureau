from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bureau import v2 as bureau_v2
from bureau.core import Dispatcher, NoEligibleTask, Registry, StateStore


def _observed_pr_dispatcher(registry, store, monkeypatch, pull_requests):
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "1")
    monkeypatch.setattr(
        bureau_v2,
        "_github_repository_for_path",
        lambda _path: "heimgewebe/grabowski",
    )
    monkeypatch.setattr(
        bureau_v2,
        "_github_open_pull_requests",
        lambda _repository: pull_requests,
    )
    return Dispatcher(registry, store)


def _add_task(root, source_task_id: str, task_id: str, *, rank: int = 1):
    task_path = root / f"registry/tasks/{source_task_id}.json"
    task = json.loads(task_path.read_text())
    task["id"] = task_id
    task["title"] = f"Task {task_id}"
    task["priority"] = {"lane": "now", "rank": rank}
    (root / f"registry/tasks/{task_id}.json").write_text(json.dumps(task))
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"]["now"].append(task_id)
    queue_path.write_text(json.dumps(queue))


def test_open_pull_request_reservation_blocks_repo_write_claim(registry_factory, tmp_path):
    root = registry_factory(1, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _: [
            bureau_v2.legacy.Reservation("open-pr:repo#999", "repo", "write-blocker", 1)
        ],
    )

    frontier = dispatcher.frontier({"repository"})
    assert frontier[0]["eligible"] is False
    assert "open-pr:repo#999" in " ".join(frontier[0]["reasons"])

    with pytest.raises(bureau_v2.legacy.NoEligibleTask) as excinfo:
        dispatcher.claim_next("worker", ("repository",))
    assert "open-pr:repo#999" in str(excinfo.value)


def test_open_pull_request_probe_failure_fails_closed_for_repo_write_claim(
    registry_factory, tmp_path
):
    root = registry_factory(1, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")

    def failing_provider(_registry):
        raise bureau_v2.OpenPullRequestObservationError("unavailable")

    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=failing_provider,
    )

    frontier = dispatcher.frontier({"repository"})
    assert frontier[0]["eligible"] is False
    assert "open PR guard failure" in " ".join(frontier[0]["reasons"])
    with pytest.raises(NoEligibleTask) as excinfo:
        dispatcher.claim_next("worker", ("repository",))
    assert "open PR guard failure" in str(excinfo.value)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:heimgewebe/bureau.git", "heimgewebe/bureau"),
        ("org-236528253@github.com:heimgewebe/vibe-lab.git", "heimgewebe/vibe-lab"),
        ("https://github.com/heimgewebe/bureau.git", "heimgewebe/bureau"),
        ("ssh://git@github.com/heimgewebe/bureau.git", "heimgewebe/bureau"),
        ("https://example.invalid/heimgewebe/bureau.git", None),
    ],
)
def test_github_repository_from_remote_url(remote, expected):
    assert bureau_v2.github_repository_from_remote_url(remote) == expected


def test_configured_github_slug_does_not_require_local_checkout_for_observation(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    resource_path = root / "registry/resources/1.json"
    resource = json.loads(resource_path.read_text())
    resource["path"] = str(root / "missing-checkout")
    resource["github_slug"] = "heimgewebe/example"
    resource_path.write_text(json.dumps(resource))
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "1")

    observed = []

    def repository_for_path(_path):
        raise AssertionError("configured github_slug must bypass local remote discovery")

    def open_pull_requests(repository):
        observed.append(repository)
        return []

    monkeypatch.setattr(bureau_v2, "_github_repository_for_path", repository_for_path)
    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", open_pull_requests)

    registry = Registry.load(root)
    reservations = bureau_v2.open_pull_request_reservations(registry)

    assert reservations == []
    assert observed == ["heimgewebe/example"]


def test_local_git_repository_without_origin_is_not_github_observation_failure(tmp_path):
    repository = tmp_path / "local-only"
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True, text=True)

    assert bureau_v2._github_repository_for_path(repository) is None


def test_origin_lookup_failure_remains_observation_failure(monkeypatch, tmp_path):
    results = iter(
        [
            subprocess.CompletedProcess([], 0, stdout=".git\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="origin\n", stderr=""),
            subprocess.CompletedProcess([], 2, stdout="", stderr="broken origin"),
        ]
    )
    monkeypatch.setattr(bureau_v2.subprocess, "run", lambda *args, **kwargs: next(results))

    with pytest.raises(
        bureau_v2.OpenPullRequestObservationError, match="cannot resolve git remote"
    ):
        bureau_v2._github_repository_for_path(tmp_path / "repository")


def test_open_pull_request_reservation_does_not_block_repo_read_claim(registry_factory, tmp_path):
    root = registry_factory(1, mode="read")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _: [
            bureau_v2.legacy.Reservation("open-pr:repo#999", "repo", "write-blocker", 1)
        ],
    )

    frontier = dispatcher.frontier({"repository"})
    assert frontier[0]["eligible"] is True
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    assert run["task_id"] == "BUR-TEST-001-T001"


def test_open_pull_request_observation_failure_is_resource_scoped(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write", max_active=2)

    repo2_path = root / "second-repo"
    repo2_path.mkdir()
    resource_path = root / "registry/resources/5.json"
    resource_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "repo2",
                "type": "git-repository",
                "parent": "root",
                "path": str(repo2_path),
            }
        )
    )
    task_path = root / "registry/tasks/BUR-TEST-001-T002.json"
    task = json.loads(task_path.read_text())
    task["claims"][0]["resource"] = "repo2"
    task_path.write_text(json.dumps(task))

    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "1")

    def repository_for_path(path):
        return "heimgewebe/healthy" if path == repo2_path else "heimgewebe/broken"

    def open_pull_requests(repository):
        if repository == "heimgewebe/broken":
            raise bureau_v2.OpenPullRequestObservationError("broken gh observation")
        return []

    monkeypatch.setattr(bureau_v2, "_github_repository_for_path", repository_for_path)
    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", open_pull_requests)

    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(registry, store)

    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    blocked_reasons = " ".join(frontier["BUR-TEST-001-T001"]["reasons"])
    eligible_reasons = " ".join(frontier["BUR-TEST-001-T002"]["reasons"])
    assert frontier["BUR-TEST-001-T001"]["eligible"] is False
    assert "repo write blocked by open PR guard failure" in blocked_reasons
    assert "repo" in blocked_reasons
    assert frontier["BUR-TEST-001-T002"]["eligible"] is True
    assert "open PR guard failure" not in eligible_reasons

    claimed = dispatcher.claim_next("worker", ("repository",))["run"]
    assert claimed["task_id"] == "BUR-TEST-001-T002"


def test_github_open_pull_requests_requests_label_metadata_and_configured_limit(
    monkeypatch,
):
    captured = {}

    class Completed:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return Completed()

    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD_LIMIT", "321")
    monkeypatch.setattr(bureau_v2.subprocess, "run", fake_run)

    assert bureau_v2._github_open_pull_requests("heimgewebe/bureau") == []
    json_fields = captured["argv"][captured["argv"].index("--json") + 1].split(",")
    assert "labels" in json_fields
    assert captured["argv"][captured["argv"].index("--limit") + 1] == "321"


def test_github_open_pull_requests_cap_reached_fails_closed(monkeypatch):
    class Completed:
        returncode = 0
        stdout = "[{\"number\":1},{\"number\":2}]"
        stderr = ""

    def fake_run(argv, **_kwargs):
        return Completed()

    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD_LIMIT", "2")
    monkeypatch.setattr(bureau_v2.subprocess, "run", fake_run)

    with pytest.raises(bureau_v2.OpenPullRequestObservationError) as excinfo:
        bureau_v2._github_open_pull_requests("heimgewebe/bureau")
    assert "BUREAU_OPEN_PR_CLAIM_GUARD_LIMIT" in str(excinfo.value)
    assert "fails closed" in str(excinfo.value)


def test_open_pr_guard_worker_count_is_bounded(monkeypatch):
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD_WORKERS", "1000")
    assert bureau_v2._open_pr_claim_guard_workers(100) == 32

    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD_WORKERS", "invalid")
    assert bureau_v2._open_pr_claim_guard_workers(100) == 8

    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD_WORKERS", "0")
    assert bureau_v2._open_pr_claim_guard_workers(3) == 3


def test_open_pr_observation_parallelizes_distinct_repositories(monkeypatch):
    barrier = threading.Barrier(2)
    calls: list[str] = []
    lock = threading.Lock()

    def observe(repository):
        with lock:
            calls.append(repository)
        barrier.wait(timeout=2)
        return [{"number": 1, "title": repository}]

    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD_WORKERS", "2")
    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", observe)

    observed, errors = bureau_v2._observe_open_pull_requests(
        ["heimgewebe/alpha", "heimgewebe/beta", "heimgewebe/alpha"]
    )

    assert errors == {}
    assert sorted(calls) == ["heimgewebe/alpha", "heimgewebe/beta"]
    assert sorted(observed) == ["heimgewebe/alpha", "heimgewebe/beta"]


def test_open_pr_observation_keeps_failures_repository_scoped(monkeypatch):
    def observe(repository):
        if repository == "heimgewebe/broken":
            raise bureau_v2.OpenPullRequestObservationError("unavailable")
        return []

    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD_WORKERS", "2")
    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", observe)

    observed, errors = bureau_v2._observe_open_pull_requests(
        ["heimgewebe/healthy", "heimgewebe/broken"]
    )

    assert observed == {"heimgewebe/healthy": []}
    assert errors == {"heimgewebe/broken": "unavailable"}


def test_open_pull_request_body_task_id_blocks_same_task(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 99,
                "title": "fix task",
                "headRefName": "fix/other-name",
                "body": "Implements BUR-TEST-001-T001 with evidence.",
                "url": "https://github.example/pr/99",
            }
        ],
    )

    reason_text = " ".join(dispatcher.frontier({"repository"})[0]["reasons"])
    assert "task already implemented by open PR" in reason_text
    assert "repo write blocked by open PR" not in reason_text
    assert "open-pr:heimgewebe/grabowski#99" in reason_text


def test_open_pull_request_task_id_scan_uses_token_boundaries(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    _add_task(root, "BUR-TEST-001-T001", "BUR-TEST-001-T0010")

    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 103,
                "title": "longer task",
                "headRefName": "feat/bur-test-001-t0010-longer-task",
                "body": "Implements BUR-TEST-001-T0010.",
                "url": "https://github.example/pr/103",
            }
        ],
    )

    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    shorter_reasons = " ".join(frontier["BUR-TEST-001-T001"]["reasons"])
    longer_reasons = " ".join(frontier["BUR-TEST-001-T0010"]["reasons"])

    assert "task already implemented by open PR" not in shorter_reasons
    assert "repo write blocked by open PR" in shorter_reasons
    assert "task already implemented by open PR" in longer_reasons


def test_open_pull_request_body_does_not_match_hyphen_extended_task_id(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    _add_task(root, "BUR-TEST-001-T001", "BUR-TEST-001-T001-EXTRA")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 105,
                "title": "extended task",
                "headRefName": "fix/no-task-id",
                "body": "Implements BUR-TEST-001-T001-EXTRA.",
                "url": "https://github.example/pr/105",
            }
        ],
    )

    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    shorter_reasons = " ".join(frontier["BUR-TEST-001-T001"]["reasons"])
    longer_reasons = " ".join(frontier["BUR-TEST-001-T001-EXTRA"]["reasons"])

    assert "task already implemented by open PR" not in shorter_reasons
    assert "task already implemented by open PR" in longer_reasons


def test_open_pull_request_branch_suffix_still_matches_shorter_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    _add_task(root, "BUR-TEST-001-T001", "BUR-TEST-001-T001-EXTRA")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 106,
                "title": "branch suffix task",
                "headRefName": "feat/bur-test-001-t001-duplicate-guard",
                "body": "No explicit body reference.",
                "url": "https://github.example/pr/106",
            }
        ],
    )

    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    shorter_reasons = " ".join(frontier["BUR-TEST-001-T001"]["reasons"])
    longer_reasons = " ".join(frontier["BUR-TEST-001-T001-EXTRA"]["reasons"])

    assert "task already implemented by open PR" in shorter_reasons
    assert "task already implemented by open PR" not in longer_reasons


def test_open_pull_request_branch_task_id_blocks_same_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 100,
                "title": "fix task",
                "headRefName": "feat/bur-test-001-t001-duplicate-guard",
                "body": "No explicit body reference.",
                "url": "https://github.example/pr/100",
            }
        ],
    )

    reasons = dispatcher.frontier({"repository"})[0]["reasons"]
    assert "task already implemented by open PR" in " ".join(reasons)
    assert "open-pr:heimgewebe/grabowski#100" in " ".join(reasons)


def test_open_pull_request_label_task_id_blocks_same_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 104,
                "title": "label task",
                "headRefName": "fix/no-task-id",
                "body": "No explicit body reference.",
                "labels": [{"name": "Bureau-Task: BUR-TEST-001-T001"}],
                "url": "https://github.example/pr/104",
            }
        ],
    )

    reasons = dispatcher.frontier({"repository"})[0]["reasons"]
    assert "task already implemented by open PR" in " ".join(reasons)


def test_open_pull_request_metadata_task_id_blocks_same_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 101,
                "title": "metadata task",
                "headRefName": "fix/no-task-id",
                "body": "No explicit body reference.",
                "metadata": {"task_id": "BUR-TEST-001-T001"},
                "url": "https://github.example/pr/101",
            }
        ],
    )

    reasons = dispatcher.frontier({"repository"})[0]["reasons"]
    assert "task already implemented by open PR" in " ".join(reasons)


def test_open_pull_request_set_metadata_task_id_blocks_same_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 107,
                "title": "set metadata task",
                "headRefName": "fix/no-task-id",
                "body": "No explicit body reference.",
                "metadata": {"task_ids": {"BUR-TEST-001-T001"}},
                "url": "https://github.example/pr/107",
            }
        ],
    )

    reasons = dispatcher.frontier({"repository"})[0]["reasons"]
    assert "task already implemented by open PR" in " ".join(reasons)


def test_open_pull_request_structured_label_overrides_branch_heuristic(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 108,
                "title": "confusing branch",
                "headRefName": "feat/bur-test-001-t002-branch-fallback",
                "body": "No explicit body reference.",
                "labels": [{"name": "Bureau-Task: BUR-TEST-001-T001"}],
                "url": "https://github.example/pr/108",
            }
        ],
    )

    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    first_reasons = " ".join(frontier["BUR-TEST-001-T001"]["reasons"])
    second_reasons = " ".join(frontier["BUR-TEST-001-T002"]["reasons"])
    assert "task already implemented by open PR" in first_reasons
    assert "task already implemented by open PR" not in second_reasons
    assert "repo write blocked by open PR" in second_reasons


def test_open_pull_request_structured_metadata_multiple_tasks_is_binding_violation(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 109,
                "title": "multi task",
                "headRefName": "fix/no-task-id",
                "body": "No broad body reference.",
                "metadata": {"bureau_tasks": ["BUR-TEST-001-T001", "BUR-TEST-001-T002"]},
                "url": "https://github.example/pr/109",
            }
        ],
    )

    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    for task_id in ("BUR-TEST-001-T001", "BUR-TEST-001-T002"):
        reasons = " ".join(frontier[task_id]["reasons"])
        assert "task already implemented by open PR" not in reasons
        assert "repo write blocked by open PR task binding violation" in reasons
        assert "binding=multiple" in reasons
        assert "task_ids=BUR-TEST-001-T001,BUR-TEST-001-T002" in reasons


def test_open_pull_request_without_task_id_is_binding_violation(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 110,
                "title": "unbound work",
                "headRefName": "fix/no-task-id",
                "body": "No Bureau task reference.",
                "url": "https://github.example/pr/110",
            }
        ],
    )

    reasons = " ".join(dispatcher.frontier({"repository"})[0]["reasons"])
    assert "repo write blocked by open PR task binding violation" in reasons
    assert "binding=missing" in reasons
    assert "task_id=missing" in reasons
    assert "open PR has no valid Bureau task binding" in reasons


def test_open_pull_request_terminal_task_binding_is_violation(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text())
    task["state"] = "verified"
    task.setdefault("metadata", {})["verification"] = {
        "task_sha256": bureau_v2.task_revision_sha256(task),
        "plan_sha256": bureau_v2.plan_sha256(Registry.load(root), task["initiative"]),
    }
    queue = json.loads((root / "registry/queue.json").read_text())
    for lane in queue["lanes"].values():
        while "BUR-TEST-001-T001" in lane:
            lane.remove("BUR-TEST-001-T001")
    (root / "registry/queue.json").write_text(json.dumps(queue))
    task_path.write_text(json.dumps(task))

    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 111,
                "title": "terminal task",
                "headRefName": "fix/no-task-id",
                "body": "Bureau-Task: BUR-TEST-001-T001",
                "url": "https://github.example/pr/111",
            }
        ],
    )

    reasons = " ".join(dispatcher.frontier({"repository"})[0]["reasons"])
    assert "repo write blocked by open PR task binding violation" in reasons
    assert "binding=terminal" in reasons
    assert "task_ids=BUR-TEST-001-T001" in reasons
    assert "open PR binds a terminal Bureau task" in reasons


def test_open_pull_request_task_binding_exception_is_not_binding_violation(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 112,
                "title": "exceptional meta work",
                "headRefName": "chore/meta-pr",
                "body": "Bureau-Task-Binding-Exception: registry-only meta PR",
                "url": "https://github.example/pr/112",
            }
        ],
    )

    reasons = " ".join(dispatcher.frontier({"repository"})[0]["reasons"])
    assert "repo write blocked by open PR: open-pr:heimgewebe/grabowski#112" in reasons
    assert "binding=exception" in reasons
    assert "registry-only meta PR" in reasons
    assert "task binding violation" not in reasons


def test_open_pull_request_task_binding_exception_label_is_schema_visible(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 113,
                "title": "label exception",
                "headRefName": "chore/meta-pr",
                "body": "No Bureau task reference.",
                "labels": [
                    {"name": "Bureau-Task-Binding-Exception: batch registry repair"}
                ],
                "url": "https://github.example/pr/113",
            }
        ],
    )

    reasons = " ".join(dispatcher.frontier({"repository"})[0]["reasons"])
    assert "binding=exception" in reasons
    assert "batch registry repair" in reasons
    assert "task binding violation" not in reasons

def test_open_pull_request_other_task_distinguishes_repo_wide_blocker(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = _observed_pr_dispatcher(
        registry,
        store,
        monkeypatch,
        [
            {
                "number": 102,
                "title": "other task",
                "headRefName": "feat/bur-test-001-t002-other-task",
                "body": "Implements BUR-TEST-001-T002.",
                "url": "https://github.example/pr/102",
            }
        ],
    )

    frontier = dispatcher.frontier({"repository"})
    first_reasons = " ".join(frontier[0]["reasons"])
    second_reasons = " ".join(frontier[1]["reasons"])
    assert "repo write blocked by open PR" in first_reasons
    assert "task already implemented by open PR" not in first_reasons
    assert "task already implemented by open PR" in second_reasons
    assert "repo write blocked by open PR" not in second_reasons


def test_three_parallel_claims_never_receive_same_task_id(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(3, mode="read", max_active=3)
    monkeypatch.setenv("BUREAU_STATE_DIR", str(tmp_path / "state"))
    registry = Registry.load(root)
    database = tmp_path / "state.sqlite3"
    StateStore(database)

    def claim(index: int) -> str:
        dispatcher = Dispatcher(registry, StateStore(database))
        return dispatcher.claim_next(f"worker-{index}", ("repository",))["run"]["task_id"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        claimed = list(pool.map(claim, range(3)))

    assert len(claimed) == 3
    assert len(set(claimed)) == 3


def test_frontier_reports_active_run_for_task(registry_factory, tmp_path):
    root = registry_factory(1, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _: [],
    )
    run = dispatcher.claim_next("worker-a", ("repository",))["run"]

    reasons = " ".join(dispatcher.frontier({"repository"})[0]["reasons"])
    assert f"active run for task {run['task_id']}" in reasons
    assert run["run_id"] in reasons


def test_existing_assignment_does_not_probe_open_pr_guard(registry_factory, tmp_path):
    root = registry_factory(1, mode="write")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    first = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _: [],
    ).claim_next("worker", ("repository",))

    def failing_provider(_registry):
        raise bureau_v2.OpenPullRequestObservationError("unavailable")

    resumed = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=failing_provider,
    ).claim_next("worker", ("repository",))

    assert resumed["status"] == "existing-assignment"
    assert resumed["run"]["run_id"] == first["run"]["run_id"]


def test_configured_git_repository_without_path_fails_closed_for_claim(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    resource_path = root / "registry/resources/1.json"
    resource = json.loads(resource_path.read_text())
    resource.pop("path", None)
    resource_path.write_text(json.dumps(resource))
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "1")

    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(registry, store)

    with pytest.raises(NoEligibleTask) as excinfo:
        dispatcher.claim_next("worker", ("repository",))
    assert "missing path" in str(excinfo.value)


def _split_repository_resources(root):
    alpha_path = root / "alpha-repo"
    beta_path = root / "beta-repo"
    alpha_path.mkdir()
    beta_path.mkdir()
    resources = {
        "1.json": {
            "schema_version": 1,
            "id": "repo",
            "type": "group",
            "parent": "root",
        },
        "2.json": {
            "schema_version": 1,
            "id": "repo.alpha",
            "type": "git-repository",
            "parent": "root",
            "path": str(alpha_path),
        },
        "3.json": {
            "schema_version": 1,
            "id": "repo.beta",
            "type": "git-repository",
            "parent": "root",
            "path": str(beta_path),
        },
    }
    for name, value in resources.items():
        (root / "registry/resources" / name).write_text(json.dumps(value))
    first_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    first = json.loads(first_path.read_text())
    first["claims"].insert(
        1,
        {
            "resource": "repo.beta",
            "mode": "write",
            "isolation": "worktree",
        },
    )
    first_path.write_text(json.dumps(first))
    return alpha_path, beta_path


def test_unbound_open_pr_is_projected_only_to_its_source_repository(
    registry_factory, tmp_path
):
    root = registry_factory(2, mode="write")
    _split_repository_resources(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    reservation = bureau_v2.OpenPullRequestReservation(
        "open-pr:heimgewebe/alpha#9",
        "repo.alpha",
        "write-blocker",
        1,
        repository="heimgewebe/alpha",
        number=9,
        task_binding_status="missing",
        task_binding_reason="open PR has no valid Bureau task binding",
        scope_resources=("repo.alpha",),
    )
    dispatcher = Dispatcher(
        registry, store, open_pr_reservations_provider=lambda _registry: [reservation]
    )

    report = dispatcher.repo_balls({"repository"})
    alpha_task = report["repo_balls"]["repo.alpha"]["lanes"]["now"][0]
    beta_task = report["repo_balls"]["repo.beta"]["lanes"]["now"][0]
    label = "open-pr:heimgewebe/alpha#9"

    assert label in " ".join(alpha_task["reasons"])
    assert label not in " ".join(beta_task["reasons"])
    assert label in " ".join(beta_task["claim_reasons"])
    assert label in " ".join(beta_task["cross_repository_reasons"])
    assert report["repo_balls"]["repo.beta"]["current_ball"]["task_id"] == (
        "BUR-TEST-001-T002"
    )

    live = dispatcher.live_conflicts({"repository"}, resource="repo.beta")
    assert all(label not in " ".join(item.get("reasons", [])) for item in live["findings"])

    what_now = dispatcher.what_now({"repository"}, resource="repo.beta", limit=10)
    assert what_now["selected"]["task_id"] == "BUR-TEST-001-T002"
    first = next(
        item for item in what_now["blocked"] if item["task_id"] == "BUR-TEST-001-T001"
    )
    assert first["blocker_reasons"] == [
        "task has blockers outside selected repository"
    ]
    assert label in " ".join(first["cross_repository_reasons"])


def test_valid_bound_pr_disjoint_source_scope_preserves_read_only_extra_claims(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    alpha_path, beta_path = _split_repository_resources(root)
    second_path = root / "registry/tasks/BUR-TEST-001-T002.json"
    second_task = json.loads(second_path.read_text())
    second_task["claims"][0].update({"resource": "repo.alpha", "mode": "write"})
    second_task["claims"].insert(
        1,
        {
            "resource": "repo.beta",
            "mode": "read",
            "isolation": "worktree",
        },
    )
    second_task["execution"]["working_repository"] = str(alpha_path)
    second_task["execution"]["grabowski_resources"] = [
        f"path:{alpha_path / 'src/feature.py'}"
    ]
    second_path.write_text(json.dumps(second_task))
    registry = Registry.load(root)
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "1")

    def repository_for_path(path):
        if path == alpha_path:
            return "heimgewebe/alpha"
        if path == beta_path:
            return "heimgewebe/beta"
        return None

    def pull_requests(repository):
        if repository == "heimgewebe/alpha":
            return [
                {
                    "number": 10,
                    "title": "implement multi-repository task",
                    "headRefName": "feat/bur-test-001-t001",
                    "body": "Bureau-Task: BUR-TEST-001-T001",
                    "url": "https://github.example/alpha/pull/10",
                    "fileScope": _t068_file_scope(
                        repository="heimgewebe/alpha",
                        number=10,
                        paths=("docs/other.md",),
                    ),
                }
            ]
        return []

    monkeypatch.setattr(bureau_v2, "_github_repository_for_path", repository_for_path)
    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", pull_requests)
    reservations = bureau_v2.open_pull_request_reservations(registry)
    reservation = next(
        item
        for item in reservations
        if isinstance(item, bureau_v2.OpenPullRequestReservation) and item.number == 10
    )

    assert reservation.scope_resources == ("repo.alpha", "repo.beta")

    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry, store, open_pr_reservations_provider=lambda _registry: reservations
    )
    beta = dispatcher.frontier({"repository"}, resource="repo.beta")
    second = next(item for item in beta if item["task_id"] == "BUR-TEST-001-T002")
    label = "open-pr:heimgewebe/alpha#10"
    assert label not in " ".join(second["reasons"])
    assert label not in " ".join(second["claim_reasons"])
    assert second["cross_repository_reasons"] == []
    assert second["resource_eligible"] is True
    assert second["eligible"] is True

    alpha = dispatcher.frontier({"repository"}, resource="repo.alpha")
    second_alpha = next(
        item for item in alpha if item["task_id"] == "BUR-TEST-001-T002"
    )
    assert label not in " ".join(second_alpha["reasons"])
    evidence = second_alpha["open_pr_scope"]["reservations"][0]
    assert evidence["classification"] == "scope-proven-disjoint"
    assert evidence["conflicting_scope_resources"] == ["repo.alpha"]


def test_bound_pr_additional_repository_scope_remains_fail_closed(
    registry_factory, tmp_path
):
    root = registry_factory(2, mode="write")
    _alpha_path, beta_path = _split_repository_resources(root)
    second_path = root / "registry/tasks/BUR-TEST-001-T002.json"
    second = json.loads(second_path.read_text())
    second["claims"] = [
        {
            "resource": "repo.beta",
            "mode": "write",
            "isolation": "worktree",
        }
    ]
    second["execution"]["working_repository"] = str(beta_path)
    second["execution"]["grabowski_resources"] = [
        f"path:{beta_path / 'src/feature.py'}"
    ]
    second_path.write_text(json.dumps(second))
    registry = Registry.load(root)
    scope = _t068_file_scope(
        repository="heimgewebe/alpha",
        number=10,
        paths=("docs/other.md",),
    )
    reservation = bureau_v2.OpenPullRequestReservation(
        "open-pr:heimgewebe/alpha#10",
        "repo.alpha",
        "write-blocker",
        1,
        repository="heimgewebe/alpha",
        number=10,
        task_ids=("BUR-TEST-001-T001",),
        task_binding_status="valid",
        task_binding_reason="bound multi-repository task",
        scope_resources=("repo.alpha", "repo.beta"),
        base_oid=scope["base_oid"],
        head_oid=scope["head_oid"],
        changed_paths=tuple(scope["changed_paths"]),
        changed_paths_sha256=scope["changed_paths_sha256"],
        changed_file_count=scope["changed_files"],
        file_scope_sha256=scope["file_scope_sha256"],
        file_scope_complete=True,
        file_scope_diagnostic="complete",
    )
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [reservation],
    )

    item = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == "BUR-TEST-001-T002"
    )
    evidence = item["open_pr_scope"]["reservations"][0]

    assert item["eligible"] is False
    assert evidence["classification"] == "repository-blocked"
    assert evidence["scope_resources"] == ["repo.alpha", "repo.beta"]
    assert evidence["conflicting_scope_resources"] == ["repo.beta"]
    assert "additional repository resources" in evidence["diagnostic"]
    assert "open-pr:heimgewebe/alpha#10" in " ".join(item["reasons"])

    projected = next(
        item
        for item in dispatcher.frontier(
            {"repository"}, resource="repo.beta"
        )
        if item["task_id"] == "BUR-TEST-001-T002"
    )
    assert "open-pr:heimgewebe/alpha#10" in " ".join(projected["reasons"])


def _t068_bind_task_scope(root, paths, *, task_id="BUR-TEST-001-T001", isolation=None):
    task_path = root / f"registry/tasks/{task_id}.json"
    task = json.loads(task_path.read_text())
    task["execution"]["grabowski_resources"] = list(paths)
    if isolation is not None:
        for claim in task["claims"]:
            if claim["mode"] in {"read", "write"}:
                claim["isolation"] = isolation
    task_path.write_text(json.dumps(task))


def _t068_file_scope(
    repository="heimgewebe/example",
    number=99,
    *,
    paths=("src/other.py",),
    base_oid="a" * 40,
    head_oid="b" * 40,
    files=None,
):
    normalized_files = (
        [{"path": item, "status": "modified"} for item in paths] if files is None else files
    )
    changed_paths = sorted(
        {
            value
            for item in normalized_files
            for value in (item["path"], item.get("previous_path"))
            if value is not None
        }
    )
    changed_paths_sha256 = bureau_v2.legacy.sha256_json(
        {
            "schema_version": bureau_v2.OPEN_PR_SCOPE_SCHEMA_VERSION,
            "changed_paths": changed_paths,
        }
    )
    material = {
        "schema_version": bureau_v2.OPEN_PR_SCOPE_SCHEMA_VERSION,
        "repository": repository,
        "number": number,
        "base_oid": base_oid,
        "head_oid": head_oid,
        "changed_files": len(normalized_files),
        "files": normalized_files,
        "changed_paths": changed_paths,
        "changed_paths_sha256": changed_paths_sha256,
    }
    return {**material, "file_scope_sha256": bureau_v2.legacy.sha256_json(material)}


def _t068_reservation(
    *,
    paths=("src/other.py",),
    number=99,
    base_oid="a" * 40,
    head_oid="b" * 40,
    binding_status="missing",
    task_ids=(),
    binding_reason="fixture binding state",
    scope_resources=("repo",),
):
    scope = _t068_file_scope(number=number, paths=paths, base_oid=base_oid, head_oid=head_oid)
    return bureau_v2.OpenPullRequestReservation(
        f"open-pr:heimgewebe/example#{number}",
        "repo",
        "write-blocker",
        1,
        repository="heimgewebe/example",
        number=number,
        branch="fix/example",
        title="example",
        url=f"https://github.example/pull/{number}",
        task_ids=tuple(task_ids),
        task_binding_status=binding_status,
        task_binding_reason=binding_reason,
        scope_resources=scope_resources,
        base_oid=base_oid,
        head_oid=head_oid,
        changed_paths=tuple(scope["changed_paths"]),
        changed_paths_sha256=scope["changed_paths_sha256"],
        changed_file_count=scope["changed_files"],
        file_scope_sha256=scope["file_scope_sha256"],
        file_scope_complete=True,
        file_scope_diagnostic="complete",
    )


def test_github_open_pull_requests_bind_complete_paginated_rename_scope(monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "pr":
            return Completed(
                json.dumps(
                    [
                        {
                            "number": 17,
                            "title": "rename",
                            "headRefName": "fix/rename",
                            "headRefOid": "b" * 40,
                            "baseRefName": "main",
                            "baseRefOid": "a" * 40,
                            "changedFiles": 2,
                            "url": "https://github.example/pull/17",
                            "body": "",
                            "labels": [],
                        }
                    ]
                )
            )
        if argv[-1].endswith("/files?per_page=100"):
            return Completed(
                json.dumps(
                    [
                        [
                            {
                                "filename": "src/new.py",
                                "previous_filename": "src/old.py",
                                "status": "renamed",
                            },
                            {"filename": "tests/test_new.py", "status": "modified"},
                        ]
                    ]
                )
            )
        return Completed(
            json.dumps(
                {
                    "state": "open",
                    "base": {"sha": "a" * 40},
                    "head": {"sha": "b" * 40},
                    "changed_files": 2,
                }
            )
        )

    monkeypatch.setattr(bureau_v2.subprocess, "run", fake_run)

    observed = bureau_v2._github_open_pull_requests("heimgewebe/example")

    assert len(calls) == 3
    assert "headRefOid" in calls[0][calls[0].index("--json") + 1]
    assert calls[1][1:4] == ["api", "--paginate", "--slurp"]
    assert calls[2][1:] == ["api", "repos/heimgewebe/example/pulls/17"]
    scope = observed[0]["fileScope"]
    assert scope["base_oid"] == "a" * 40
    assert scope["head_oid"] == "b" * 40
    assert scope["changed_paths"] == ["src/new.py", "src/old.py", "tests/test_new.py"]
    complete, normalized, diagnostic = bureau_v2._file_scope_from_pull_request(
        observed[0], "heimgewebe/example", 17
    )
    assert complete is True
    assert normalized == scope
    assert diagnostic == "complete"


def test_github_open_pull_requests_reject_invalid_oid_before_file_read(monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            [
                {
                    "number": 17,
                    "title": "bad oid",
                    "headRefName": "fix/bad",
                    "headRefOid": "not-an-oid",
                    "baseRefName": "main",
                    "baseRefOid": "a" * 40,
                    "url": "https://github.example/pull/17",
                    "body": "",
                    "labels": [],
                }
            ]
        )

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return Completed()

    monkeypatch.setattr(bureau_v2.subprocess, "run", fake_run)

    with pytest.raises(bureau_v2.OpenPullRequestObservationError, match="invalid head OID"):
        bureau_v2._github_open_pull_requests("heimgewebe/example")
    assert len(calls) == 1


@pytest.mark.parametrize("payload", ["{}", "[{}]", "not-json"])
def test_github_pull_request_file_pagination_fails_closed(monkeypatch, payload):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = payload

    monkeypatch.setattr(bureau_v2.subprocess, "run", lambda *_args, **_kwargs: Completed())

    with pytest.raises(bureau_v2.OpenPullRequestObservationError):
        bureau_v2._github_pull_request_file_scope(
            "heimgewebe/example",
            17,
            base_oid="a" * 40,
            head_oid="b" * 40,
            expected_changed_files=1,
        )


def test_github_pull_request_file_cap_reached_fails_closed(monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps([[{"filename": "src/file.py", "status": "modified"}]])

    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD_FILE_LIMIT", "1")
    monkeypatch.setattr(bureau_v2.subprocess, "run", lambda *_args, **_kwargs: Completed())

    with pytest.raises(bureau_v2.OpenPullRequestObservationError, match="FILE_LIMIT"):
        bureau_v2._github_pull_request_file_scope(
            "heimgewebe/example",
            17,
            base_oid="a" * 40,
            head_oid="b" * 40,
            expected_changed_files=1,
        )


def test_file_scope_reader_rejects_files_changed_path_disagreement():
    scope = _t068_file_scope(paths=("src/one.py",))
    scope["files"] = [{"path": "src/two.py", "status": "modified"}]
    material = {key: value for key, value in scope.items() if key != "file_scope_sha256"}
    scope["file_scope_sha256"] = bureau_v2.legacy.sha256_json(material)

    complete, _normalized, diagnostic = bureau_v2._file_scope_from_pull_request(
        {"fileScope": scope}, "heimgewebe/example", 99
    )

    assert complete is False
    assert diagnostic == "open PR files and changed paths disagree"


@pytest.mark.parametrize(
    ("task_path", "pr_path", "eligible", "classification"),
    [
        ("src/feature.py", "src/other.py", True, "scope-proven-disjoint"),
        ("src/feature.py", "src/feature.py", False, "scope-conflict"),
        ("src/package", "src/package/module.py", False, "scope-conflict"),
        ("src/package/module.py", "src/package", False, "scope-conflict"),
        ("src/foo", "src/foobar", True, "scope-proven-disjoint"),
    ],
)
def test_open_pr_scope_uses_path_segments(
    registry_factory, tmp_path, task_path, pr_path, eligible, classification
):
    root = registry_factory(1, mode="write")
    _t068_bind_task_scope(root, [f"path:{root / task_path}"])
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [_t068_reservation(paths=(pr_path,))],
    )

    item = dispatcher.frontier({"repository"})[0]

    assert item["eligible"] is eligible
    assert item["open_pr_scope"]["reservations"][0]["classification"] == classification
    if eligible:
        assert "repo write blocked by open PR" not in " ".join(item["reasons"])
    else:
        assert "repo write blocked by open PR" in " ".join(item["reasons"])


@pytest.mark.parametrize(
    "scope_kind", ["missing", "broad", "broad-alias", "outside"]
)
def test_open_pr_scope_requires_exact_in_repository_paths(registry_factory, tmp_path, scope_kind):
    root = registry_factory(1, mode="write")
    if scope_kind == "broad":
        _t068_bind_task_scope(root, [f"repo:{root.resolve()}"])
    elif scope_kind == "broad-alias":
        _t068_bind_task_scope(root, [f"repo:{root.resolve()}/."])
    elif scope_kind == "outside":
        _t068_bind_task_scope(root, ["path:/tmp/t068-outside.py"])
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [_t068_reservation()],
    )

    item = dispatcher.frontier({"repository"})[0]

    assert item["eligible"] is False
    reservation = item["open_pr_scope"]["reservations"][0]
    assert reservation["classification"] == "scope-required"
    assert "classification=scope-required" in " ".join(item["reasons"])


def test_binding_violation_remains_visible_but_disjoint_scope_is_claimable(
    registry_factory, tmp_path
):
    root = registry_factory(1, mode="write")
    _t068_bind_task_scope(root, [f"path:{root / 'src/feature.py'}"])
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [
            _t068_reservation(paths=("docs/other.md",), binding_status="missing")
        ],
    )

    item = dispatcher.frontier({"repository"})[0]
    reservation = item["open_pr_scope"]["reservations"][0]

    assert item["eligible"] is True
    assert reservation["classification"] == "scope-proven-disjoint"
    assert reservation["task_binding_status"] == "missing"


def test_claim_intent_replaces_broad_repository_lease_with_exact_scope_paths(
    registry_factory, tmp_path
):
    root = registry_factory(1, mode="write")
    task_id = "BUR-TEST-001-T001"
    exact_path = root / "src/feature.py"
    _t068_bind_task_scope(
        root,
        [f"path:{exact_path}"],
        task_id=task_id,
        isolation="none",
    )
    resource_path = next(
        path
        for path in (root / "registry/resources").glob("*.json")
        if json.loads(path.read_text()).get("id") == "repo"
    )
    resource = json.loads(resource_path.read_text())
    resource["grabowski_key"] = f"repo:{root.resolve()}"
    resource_path.write_text(json.dumps(resource))
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [
            _t068_reservation(paths=("docs/other.md",))
        ],
    )

    result = dispatcher.claim_intent(
        "worker-exact-resource-scope",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="test-t068-resource-scope",
    )

    assert result["intent"]["required_resource_keys"] == [f"path:{exact_path}"]
    assert result["ready_supply"]["open_pr_scope_resource_mode"] == "exact-paths"
    assert f"repo:{root.resolve()}" not in result["intent"]["required_resource_keys"]


def test_claim_intent_binds_nonconflict_and_commit_rejects_pr_head_drift(
    registry_factory, tmp_path
):
    root = registry_factory(1, mode="write")
    task_id = "BUR-TEST-001-T001"
    _t068_bind_task_scope(
        root,
        [f"path:{root / 'src/feature.py'}"],
        task_id=task_id,
        isolation="none",
    )
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    current = [_t068_reservation(paths=("docs/other.md",), head_oid="b" * 40)]
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: list(current),
    )

    result = dispatcher.claim_intent(
        "worker-t068",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="test-t068",
    )
    intent = result["intent"]
    proof = intent["operator_approval"]["open_pr_nonconflict"]

    assert proof["requires_evidence"] is True
    assert proof["reservations"][0]["classification"] == "scope-proven-disjoint"
    assert proof["reservations"][0]["head_oid"] == "b" * 40
    current[:] = [_t068_reservation(paths=("docs/other.md",), head_oid="c" * 40)]

    with pytest.raises(
        bureau_v2.legacy.StateError,
        match="open PR nonconflict evidence changed after intent",
    ):
        dispatcher.commit_claim_intent(
            intent,
            {"owner_id": intent["lease_owner_id"], "task_id": task_id},
        )

    with store.connect() as connection:
        assert store.active_runs(connection) == []


def test_claim_commit_rejects_disjoint_pr_task_binding_drift(
    registry_factory, tmp_path
):
    root = registry_factory(3, mode="write")
    task_id = "BUR-TEST-001-T001"
    _t068_bind_task_scope(
        root,
        [f"path:{root / 'src/feature.py'}"],
        task_id=task_id,
        isolation="none",
    )
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    current = [
        _t068_reservation(
            paths=("docs/other.md",),
            binding_status="valid",
            task_ids=("BUR-TEST-001-T002",),
            binding_reason="bound to second task",
        )
    ]
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: list(current),
    )

    intent = dispatcher.claim_intent(
        "worker-binding-drift",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="test-t068-binding-drift",
    )["intent"]
    reservation = intent["operator_approval"]["open_pr_nonconflict"][
        "reservations"
    ][0]
    assert reservation["classification"] == "scope-proven-disjoint"
    assert reservation["task_ids"] == ["BUR-TEST-001-T002"]
    assert reservation["task_binding_reason"] == "bound to second task"

    current[:] = [
        _t068_reservation(
            paths=("docs/other.md",),
            binding_status="valid",
            task_ids=("BUR-TEST-001-T003",),
            binding_reason="bound to third task",
        )
    ]

    with pytest.raises(
        bureau_v2.legacy.StateError,
        match="open PR nonconflict evidence changed after intent",
    ):
        dispatcher.commit_claim_intent(
            intent,
            {"owner_id": intent["lease_owner_id"], "task_id": task_id},
        )

    with store.connect() as connection:
        assert store.active_runs(connection) == []


def test_github_pull_request_file_count_mismatch_fails_closed(monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps([[{"filename": "src/file.py", "status": "modified"}]])

    monkeypatch.setattr(bureau_v2.subprocess, "run", lambda *_args, **_kwargs: Completed())

    with pytest.raises(bureau_v2.OpenPullRequestObservationError, match="changedFiles mismatch"):
        bureau_v2._github_pull_request_file_scope(
            "heimgewebe/example",
            17,
            base_oid="a" * 40,
            head_oid="b" * 40,
            expected_changed_files=0,
        )


def test_open_pr_file_scope_overrides_disjoint_declared_components(
    registry_factory, tmp_path
):
    root = registry_factory(1, mode="write")
    resources = root / "registry/resources"
    (resources / "component-a.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "component.a",
                "type": "component",
                "parent": "repo",
                "path": str(root / "src/a"),
                "grabowski_key": str(root / ".scope-a"),
            }
        )
    )
    (resources / "component-b.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "component.b",
                "type": "component",
                "parent": "repo",
                "path": str(root / "src/b"),
                "grabowski_key": str(root / ".scope-b"),
            }
        )
    )
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text())
    task["claims"][0]["resource"] = "component.a"
    task["execution"]["grabowski_resources"] = [
        f"path:{root / 'src/feature.py'}"
    ]
    task_path.write_text(json.dumps(task))
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [
            _t068_reservation(
                paths=("src/feature.py",), scope_resources=("component.b",)
            )
        ],
    )

    item = dispatcher.frontier({"repository"})[0]

    assert item["eligible"] is False
    reservation = item["open_pr_scope"]["reservations"][0]
    assert reservation["classification"] == "scope-conflict"
    assert "classification=scope-conflict" in " ".join(item["reasons"])


def test_commit_rejects_unexpected_nonconflict_evidence(registry_factory, tmp_path):
    root = registry_factory(1, mode="write")
    task_id = "BUR-TEST-001-T001"
    _t068_bind_task_scope(
        root,
        [f"path:{root / 'src/feature.py'}"],
        task_id=task_id,
        isolation="none",
    )
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [],
    )
    result = dispatcher.claim_intent(
        "worker-unexpected-proof",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="test-t068",
    )
    intent = result["intent"]
    task = registry.tasks[task_id]
    proof = bureau_v2._task_open_pr_scope_assessment(task, [], registry)
    assert proof["requires_evidence"] is False
    intent["operator_approval"]["open_pr_nonconflict"] = proof
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)

    with pytest.raises(
        bureau_v2.legacy.StateError,
        match="intent differs from issued identity",
    ):
        dispatcher.commit_claim_intent(intent, None)

    with store.connect() as connection:
        assert store.active_runs(connection) == []




def test_github_open_pull_requests_reject_identity_drift_after_file_read(monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "pr":
            return Completed(
                json.dumps(
                    [
                        {
                            "number": 17,
                            "title": "drift",
                            "headRefName": "fix/drift",
                            "headRefOid": "b" * 40,
                            "baseRefName": "main",
                            "baseRefOid": "a" * 40,
                            "changedFiles": 1,
                            "url": "https://github.example/pull/17",
                            "body": "",
                            "labels": [],
                        }
                    ]
                )
            )
        if argv[-1].endswith("/files?per_page=100"):
            return Completed(
                json.dumps([[{"filename": "src/file.py", "status": "modified"}]])
            )
        return Completed(
            json.dumps(
                {
                    "state": "open",
                    "base": {"sha": "a" * 40},
                    "head": {"sha": "c" * 40},
                    "changed_files": 1,
                }
            )
        )

    monkeypatch.setattr(bureau_v2.subprocess, "run", fake_run)

    with pytest.raises(
        bureau_v2.OpenPullRequestObservationError,
        match="identity changed during file observation",
    ):
        bureau_v2._github_open_pull_requests("heimgewebe/example")
    assert len(calls) == 3



@pytest.mark.parametrize("alias_kind", ["dotdot", "symlink", "hardlink"])
def test_open_pr_scope_rejects_filesystem_aliases(
    registry_factory, tmp_path, alias_kind
):
    root = registry_factory(1, mode="write")
    src = root / "src"
    src.mkdir(exist_ok=True)
    target = src / "target.py"
    target.write_text("value = 1\n")
    if alias_kind == "dotdot":
        resource_path = f"{src}/../src/target.py"
    elif alias_kind == "symlink":
        link = src / "link.py"
        link.symlink_to(target.name)
        resource_path = str(link)
    else:
        link = src / "hardlink.py"
        link.hardlink_to(target)
        resource_path = str(link)
    _t068_bind_task_scope(root, [f"path:{resource_path}"])
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [
            _t068_reservation(paths=("docs/other.md",))
        ],
    )

    item = dispatcher.frontier({"repository"})[0]

    assert item["eligible"] is False
    reservation = item["open_pr_scope"]["reservations"][0]
    assert reservation["classification"] == "scope-required"
    assert "classification=scope-required" in " ".join(item["reasons"])



def test_open_pr_scope_rejects_mixed_inside_and_outside_paths(
    registry_factory, tmp_path
):
    root = registry_factory(1, mode="write")
    inside = root / "src" / "feature.py"
    inside.parent.mkdir(exist_ok=True)
    inside.write_text("value = 1\n")
    _t068_bind_task_scope(
        root,
        [f"path:{inside}", "path:/tmp/t068-outside-alias-candidate"],
    )
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [
            _t068_reservation(paths=("docs/other.md",))
        ],
    )

    item = dispatcher.frontier({"repository"})[0]

    assert item["eligible"] is False
    reservation = item["open_pr_scope"]["reservations"][0]
    assert reservation["classification"] == "scope-required"
    assert "outside working repository" in reservation["diagnostic"]


_ADOPTION_REPOSITORY = "heimgewebe/example"
_ADOPTION_PR = 77
_ADOPTION_BASE = "a" * 40
_ADOPTION_HEAD = "b" * 40


def _open_pr_adoption_reservation(
    *,
    number=_ADOPTION_PR,
    base_oid=_ADOPTION_BASE,
    head_oid=_ADOPTION_HEAD,
    task_ids=("BUR-TEST-001-T001",),
    observation_failed=False,
    scope_resources=("repo",),
):
    return bureau_v2.OpenPullRequestReservation(
        f"open-pr:{_ADOPTION_REPOSITORY}#{number}",
        "repo",
        "write-blocker",
        1,
        repository=_ADOPTION_REPOSITORY,
        number=number,
        task_ids=task_ids,
        task_binding_status="valid" if not observation_failed else "unknown",
        task_binding_reason=(
            "open PR binds exactly one open Bureau task"
            if not observation_failed
            else "open PR observation failed"
        ),
        scope_resources=scope_resources,
        base_oid=base_oid,
        head_oid=head_oid,
        changed_paths=("src/existing.py",),
        changed_paths_sha256="c" * 64,
        changed_file_count=1,
        file_scope_sha256="d" * 64,
        file_scope_complete=not observation_failed,
        file_scope_diagnostic=("complete" if not observation_failed else "GitHub unavailable"),
        observation_failed=observation_failed,
    )


def _configure_open_pr_adoption_registry(root, *, variant="exact"):
    implementation_id = "BUR-TEST-001-T001"
    merge_id = "BUR-TEST-001-T002"
    implementation_path = root / f"registry/tasks/{implementation_id}.json"
    implementation = json.loads(implementation_path.read_text())
    implementation["state"] = "ready" if variant == "unverified-predecessor" else "verified"
    implementation_revision = {
        "schema_version": 1,
        "repository": _ADOPTION_REPOSITORY,
        "pull_request": _ADOPTION_PR,
        "base_sha": _ADOPTION_BASE,
        "head_sha": (
            "c" * 40 if variant == "predecessor-head-drift" else _ADOPTION_HEAD
        ),
    }
    implementation_metadata = {}
    if variant != "missing-implementation-revision":
        implementation_metadata["open_pr_implementation"] = implementation_revision
    if implementation["state"] == "verified":
        implementation["metadata"] = implementation_metadata
        implementation_metadata["verification"] = {
            "task_sha256": bureau_v2.task_revision_sha256(implementation),
            "plan_sha256": bureau_v2.legacy.sha256_json({}),
        }
    if implementation_metadata:
        implementation["metadata"] = implementation_metadata
    implementation_path.write_text(json.dumps(implementation))
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    for lane in queue["lanes"].values():
        while implementation_id in lane:
            lane.remove(implementation_id)
    queue_path.write_text(json.dumps(queue))

    merge_path = root / f"registry/tasks/{merge_id}.json"
    merge = json.loads(merge_path.read_text())
    merge["depends_on"] = [] if variant == "missing-dependency" else [implementation_id]
    merge["claims"] = [
        {
            "resource": "repo",
            "mode": "write" if variant == "nonexclusive" else "exclusive",
            "amount": 1,
            "isolation": "worktree",
        }
    ]
    merge["execution"]["approval"] = {
        "required_level": "operator",
        "action_class": "repository_mutation",
        "note": "test exact open PR adoption",
    }
    contract = {
        "schema_version": 1,
        "repository": _ADOPTION_REPOSITORY,
        "pull_request": _ADOPTION_PR,
        "base_sha": _ADOPTION_BASE,
        "head_sha": _ADOPTION_HEAD,
        "implementation_task": implementation_id,
        "merge_task": merge_id,
    }
    if variant == "wrong-implementation-task":
        contract["implementation_task"] = "BUR-TEST-001-T999"
    elif variant == "wrong-pr":
        contract["pull_request"] = _ADOPTION_PR + 1
    elif variant == "base-drift":
        contract["base_sha"] = "e" * 40
    elif variant == "head-drift":
        contract["head_sha"] = "f" * 40
    elif variant == "extra-contract-field":
        contract["unexpected"] = True
    merge["metadata"] = {"open_pr_adoption": contract}
    merge_path.write_text(json.dumps(merge))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Bureau Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "test baseline"], cwd=root, check=True)
    return Registry.load(root)


def test_exact_open_pr_merge_adoption_is_claimable(registry_factory, tmp_path):
    root = registry_factory(2, mode="write")
    registry = _configure_open_pr_adoption_registry(root)
    reservation = _open_pr_adoption_reservation()
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [reservation],
    )

    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    merge = frontier["BUR-TEST-001-T002"]

    assert merge["eligible"] is True
    assert merge["reasons"] == []
    assessment = merge["open_pr_scope"]
    assert assessment["classification_counts"]["merge-adopted"] == 1
    assert assessment["requires_evidence"] is False
    assert assessment["reservations"][0]["classification"] == "merge-adopted"
    claimed = dispatcher.claim_next("worker-adoption", ("repository",))["run"]
    assert claimed["task_id"] == "BUR-TEST-001-T002"


@pytest.mark.parametrize(
    "variant",
    [
        "wrong-implementation-task",
        "nonexclusive",
        "unverified-predecessor",
        "missing-dependency",
        "wrong-pr",
        "missing-implementation-revision",
        "base-drift",
        "head-drift",
        "extra-contract-field",
    ],
)
def test_open_pr_merge_adoption_contract_failures_remain_blocking(
    registry_factory, tmp_path, variant
):
    root = registry_factory(2, mode="write")
    registry = _configure_open_pr_adoption_registry(root, variant=variant)
    reservation = _open_pr_adoption_reservation()
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [reservation],
    )

    merge = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == "BUR-TEST-001-T002"
    )

    assert merge["eligible"] is False
    assert merge["open_pr_scope"]["classification_counts"]["merge-adopted"] == 0
    assert "open-pr:heimgewebe/example#77" in " ".join(merge["reasons"])


def test_open_pr_merge_adoption_rejects_predecessor_verified_for_old_head(
    registry_factory, tmp_path
):
    root = registry_factory(2, mode="write")
    registry = _configure_open_pr_adoption_registry(
        root, variant="predecessor-head-drift"
    )
    reservation = _open_pr_adoption_reservation()
    predecessor = registry.tasks["BUR-TEST-001-T001"]
    verification = predecessor.raw["metadata"]["verification"]
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [reservation],
    )

    merge = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == "BUR-TEST-001-T002"
    )

    assert predecessor.raw["metadata"]["open_pr_implementation"]["head_sha"] == "c" * 40
    assert verification["task_sha256"] == predecessor.sha256
    assert reservation.head_oid == _ADOPTION_HEAD
    assert merge["eligible"] is False
    assert merge["open_pr_scope"]["classification_counts"]["merge-adopted"] == 0
    assert "open-pr:heimgewebe/example#77" in " ".join(merge["reasons"])


def test_open_pr_merge_adoption_rejects_additional_repository_scope(
    registry_factory, tmp_path
):
    root = registry_factory(2, mode="write")
    registry = _configure_open_pr_adoption_registry(root)
    reservation = _open_pr_adoption_reservation(scope_resources=("repo", "repo.beta"))
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [reservation],
    )

    merge = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == "BUR-TEST-001-T002"
    )

    assert merge["eligible"] is False
    assert merge["open_pr_scope"]["classification_counts"]["merge-adopted"] == 0


def test_open_pr_merge_adoption_does_not_hide_additional_pr(
    registry_factory, tmp_path
):
    root = registry_factory(3, mode="write")
    registry = _configure_open_pr_adoption_registry(root)
    adopted = _open_pr_adoption_reservation()
    additional = _open_pr_adoption_reservation(
        number=78,
        task_ids=("BUR-TEST-001-T003",),
    )
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [adopted, additional],
    )

    merge = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == "BUR-TEST-001-T002"
    )
    by_number = {
        item["number"]: item for item in merge["open_pr_scope"]["reservations"]
    }

    assert merge["eligible"] is False
    assert by_number[77]["classification"] == "merge-adopted"
    assert by_number[78]["classification"] != "merge-adopted"
    assert "open-pr:heimgewebe/example#78" in " ".join(merge["reasons"])
    assert "open-pr:heimgewebe/example#77" not in " ".join(merge["reasons"])


def test_open_pr_merge_adoption_observation_failure_blocks(registry_factory, tmp_path):
    root = registry_factory(2, mode="write")
    registry = _configure_open_pr_adoption_registry(root)
    reservation = _open_pr_adoption_reservation(observation_failed=True)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [reservation],
    )

    merge = next(
        item
        for item in dispatcher.frontier({"repository"})
        if item["task_id"] == "BUR-TEST-001-T002"
    )

    assert merge["eligible"] is False
    assert "open PR guard failure" in " ".join(merge["reasons"])


def test_open_pr_merge_adoption_claim_commit_rejects_head_drift(
    registry_factory, tmp_path
):
    root = registry_factory(2, mode="write")
    registry = _configure_open_pr_adoption_registry(root)
    current = [_open_pr_adoption_reservation()]
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: list(current),
    )

    result = dispatcher.claim_intent(
        "worker-adoption-drift",
        ("repository",),
        task_id="BUR-TEST-001-T002",
        approved=True,
        approval_source="test-open-pr-adoption",
    )
    intent = result["intent"]
    assessment = result["ready_supply"]["open_pr_scope"]
    assert assessment["reservations"][0]["classification"] == "merge-adopted"
    assert "open_pr_nonconflict" not in intent["operator_approval"]

    current[:] = [_open_pr_adoption_reservation(head_oid="c" * 40)]

    with pytest.raises(
        bureau_v2.legacy.StateError,
        match="coordinated claim eligibility changed",
    ):
        dispatcher.commit_claim_intent(intent, None)

    with store.connect() as connection:
        assert store.active_runs(connection) == []


def _redirect_registry(registry_factory, monkeypatch, resolver):
    """Registry with repo.lenskit as a GitHub redirect onto repo.repoground."""
    root = registry_factory(1, mode="write")
    for name, slug in (("lenskit", "heimgewebe/lenskit"), ("repoground", "heimgewebe/repoground")):
        (root / f"registry/resources/{name}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": f"repo.{name}",
                    "type": "git-repository",
                    "parent": "root",
                    "path": str(root / name),
                    "github_slug": slug,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "1")
    monkeypatch.setattr(bureau_v2, "_github_repository_for_path", lambda _path: None)
    monkeypatch.setattr(
        bureau_v2.github_repository, "github_canonical_slug", resolver
    )
    return root


def _redirected_pull_requests(repository):
    """Both slugs resolve server-side to the same canonical repository."""
    if repository not in {"heimgewebe/lenskit", "heimgewebe/repoground"}:
        return []
    return [
        {
            "number": 1130,
            "title": "repoground change",
            "headRefName": "feat/repoground",
            "body": "Bureau-Task: BUR-TEST-001-T001",
            "url": "https://github.com/heimgewebe/repoground/pull/1130",
        }
    ]


def test_github_redirect_slug_does_not_create_a_second_pr_reservation(
    registry_factory, monkeypatch
):
    root = _redirect_registry(
        registry_factory,
        monkeypatch,
        lambda slug: "heimgewebe/repoground",
    )
    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", _redirected_pull_requests)

    registry = Registry.load(root)
    reservations = bureau_v2.open_pull_request_reservations(registry)

    pr_reservations = [
        item
        for item in reservations
        if isinstance(item, bureau_v2.OpenPullRequestReservation) and item.number == 1130
    ]
    assert len(pr_reservations) == 1
    reservation = pr_reservations[0]
    assert reservation.repository == "heimgewebe/repoground"
    assert reservation.resource == "repo.repoground"

    rendered = json.dumps([item.__dict__ for item in reservations], default=str)
    assert "heimgewebe/lenskit" not in rendered
    assert not any(
        isinstance(item, bureau_v2.OpenPullRequestReservation) and item.observation_failed
        for item in reservations
    )


def test_github_redirect_provider_failure_blocks_instead_of_guessing(
    registry_factory, monkeypatch
):
    def failing(slug):
        raise bureau_v2.github_repository.RepositoryCanonicalIdentityError(
            "canonical-identity-unavailable", f"gh repo view failed for {slug}"
        )

    root = _redirect_registry(registry_factory, monkeypatch, failing)
    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", _redirected_pull_requests)

    registry = Registry.load(root)
    reservations = bureau_v2.open_pull_request_reservations(registry)

    blocked = {
        item.resource
        for item in reservations
        if isinstance(item, bureau_v2.OpenPullRequestReservation) and item.observation_failed
    }
    assert blocked == {"repo.lenskit", "repo.repoground"}
    assert not any(
        isinstance(item, bureau_v2.OpenPullRequestReservation) and item.number == 1130
        for item in reservations
    )


def test_pull_requests_contradicting_the_observed_identity_block(registry_factory, monkeypatch):
    root = _redirect_registry(
        registry_factory,
        monkeypatch,
        lambda slug: "heimgewebe/repoground",
    )

    def contradictory(repository):
        if repository != "heimgewebe/repoground":
            return []
        return [
            {
                "number": 1130,
                "title": "contradictory",
                "headRefName": "feat/x",
                "body": "Bureau-Task: BUR-TEST-001-T001",
                "url": "https://github.com/heimgewebe/somewhere-else/pull/1130",
            }
        ]

    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", contradictory)

    registry = Registry.load(root)
    reservations = bureau_v2.open_pull_request_reservations(registry)

    failures = [
        item
        for item in reservations
        if isinstance(item, bureau_v2.OpenPullRequestReservation)
        and item.observation_failed
        and item.resource == "repo.repoground"
    ]
    assert len(failures) == 1
    assert "conflicting" in failures[0].diagnostic
