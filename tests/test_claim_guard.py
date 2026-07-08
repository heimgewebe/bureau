from __future__ import annotations

import json
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
        ("https://github.com/heimgewebe/bureau.git", "heimgewebe/bureau"),
        ("ssh://git@github.com/heimgewebe/bureau.git", "heimgewebe/bureau"),
        ("https://example.invalid/heimgewebe/bureau.git", None),
    ],
)
def test_github_repository_from_remote_url(remote, expected):
    assert bureau_v2.github_repository_from_remote_url(remote) == expected


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


def test_github_open_pull_requests_requests_label_metadata(monkeypatch):
    captured = {}

    class Completed:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return Completed()

    monkeypatch.setattr(bureau_v2.subprocess, "run", fake_run)

    assert bureau_v2._github_open_pull_requests("heimgewebe/bureau") == []
    json_fields = captured["argv"][captured["argv"].index("--json") + 1].split(",")
    assert "labels" in json_fields


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

    reasons = dispatcher.frontier({"repository"})[0]["reasons"]
    assert "task already implemented by open PR" in " ".join(reasons)
    assert "open-pr:heimgewebe/grabowski#99" in " ".join(reasons)


def test_open_pull_request_task_id_scan_uses_token_boundaries(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    longer_task = json.loads(task_path.read_text())
    longer_task["id"] = "BUR-TEST-001-T0010"
    longer_task["priority"] = {"lane": "now", "rank": 1}
    (root / "registry/tasks/BUR-TEST-001-T0010.json").write_text(
        json.dumps(longer_task)
    )
    queue = json.loads((root / "registry/queue.json").read_text())
    queue["lanes"]["now"].append("BUR-TEST-001-T0010")
    (root / "registry/queue.json").write_text(json.dumps(queue))

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
                "labels": [{"name": "BUR-TEST-001-T001"}],
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
    assert "repo write blocked by open PR" in second_reasons


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
