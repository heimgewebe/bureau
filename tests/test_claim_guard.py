from __future__ import annotations

import json

import pytest

from bureau import v2 as bureau_v2
from bureau.core import Dispatcher, Registry, StateError, StateStore


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


def test_open_pull_request_probe_failure_fails_closed_for_claim(registry_factory, tmp_path):
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

    assert dispatcher.frontier({"repository"})[0]["eligible"] is True
    with pytest.raises(StateError, match="open pull request guard failed: unavailable"):
        dispatcher.claim_next("worker", ("repository",))


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

    with pytest.raises(StateError, match="missing path"):
        dispatcher.claim_next("worker", ("repository",))
