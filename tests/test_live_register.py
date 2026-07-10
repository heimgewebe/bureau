from __future__ import annotations

import json

import pytest

from bureau import cli as bureau_cli
from bureau.core import Registry, StateError, StateStore
from bureau.live_register import live_register_list, live_register_record


def setup_live(registry_factory, tmp_path):
    root = registry_factory(2)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    return root, registry, store


def test_live_register_records_thread_focus_without_mutating_queue(registry_factory, tmp_path):
    root, registry, store = setup_live(registry_factory, tmp_path)
    before = (root / "registry/queue.json").read_text(encoding="utf-8")

    result = live_register_record(
        registry,
        store,
        kind="thread_focus",
        thread_id="chat-20260710-a",
        repo="repo.alpha",
        title="Inspect alpha work",
        source="chat",
        note="operator focus only",
    )

    assert result["record"]["kind"] == "thread_focus"
    assert result["record"]["repo"] == "repo.alpha"
    assert result["record"]["thread_id"] == "chat-20260710-a"
    assert "queue_truth" in result["nonclaims"]
    assert (root / "registry/queue.json").read_text(encoding="utf-8") == before

    listed = live_register_list(store)
    assert listed["summary"]["active_thread_focus_count"] == 1
    assert listed["summary"]["active_thread_focus"][0]["record"]["title"] == "Inspect alpha work"


def test_live_register_records_candidate_needing_promotion(registry_factory, tmp_path):
    _root, registry, store = setup_live(registry_factory, tmp_path)

    live_register_record(
        registry,
        store,
        kind="candidate_task",
        title="Potential beta follow-up",
        repo="repo.beta",
        source="chat",
        promotion_required=True,
    )

    listed = live_register_list(store, kind="candidate_task")
    assert listed["summary"]["open_candidate_count"] == 1
    assert listed["summary"]["promotion_required_count"] == 1
    promotion = listed["summary"]["promotion_required"][0]
    assert promotion["record"]["title"] == "Potential beta follow-up"


def test_live_register_rejects_unknown_repo_resource(registry_factory, tmp_path):
    _root, registry, store = setup_live(registry_factory, tmp_path)

    with pytest.raises(StateError, match="unknown live register repo resource"):
        live_register_record(
            registry,
            store,
            kind="thread_focus",
            thread_id="chat-1",
            repo="repo.unknown",
            title="Bad repo",
        )


def test_live_register_cli_writes_and_lists_state_events(registry_factory, tmp_path, capsys):
    root, _registry, _store = setup_live(registry_factory, tmp_path)
    state_root = tmp_path / "cli-state"

    rc = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(state_root),
            "--json",
            "live-register",
            "--kind",
            "thread_focus",
            "--thread-id",
            "chat-cli",
            "--repo",
            "repo.alpha",
            "--title",
            "CLI focus",
            "--source",
            "chat",
        ]
    )
    assert rc == 0
    written = json.loads(capsys.readouterr().out)
    assert written["record"]["title"] == "CLI focus"

    rc = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(state_root),
            "--json",
            "live-list",
            "--thread-id",
            "chat-cli",
        ]
    )
    assert rc == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["summary"]["active_thread_focus_count"] == 1
    assert listed["records"][0]["record"]["repo"] == "repo.alpha"
