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
