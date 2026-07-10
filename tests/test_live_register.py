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


def test_live_register_worker_id_and_export_retention(registry_factory, tmp_path):
    _root, registry, store = setup_live(registry_factory, tmp_path)

    live_register_record(
        registry,
        store,
        kind="thread_focus",
        thread_id="chat-worker",
        worker_id="worker-alpha",
        repo="repo.alpha",
        title="Worker focus",
        note="private details stay out of chronik export body",
    )

    from bureau.live_register import live_register_export, live_retention_report

    exported = live_register_export(store, export_format="chronik")
    assert exported["records"][0]["worker_id"] == "worker-alpha"
    assert "payload_digest" in exported["records"][0]
    assert "note" not in exported["records"][0]
    assert "unredacted_export" in exported["does_not_establish"]

    retention = live_retention_report(store)
    assert retention["delete_authority"] is False
    assert retention["summary"]["by_kind"] == {"thread_focus": 1}


def test_live_promote_plan_requires_review_and_applies_task_file(
    registry_factory, tmp_path,
):
    root, registry, store = setup_live(registry_factory, tmp_path)
    result = live_register_record(
        registry,
        store,
        kind="candidate_task",
        repo="repo.alpha",
        title="Promoted candidate",
        promotion_required=True,
    )
    plan_path = tmp_path / "promote.json"

    from bureau.live_register import apply_live_promote_plan, write_live_promote_plan

    plan = write_live_promote_plan(
        registry,
        store,
        event_id=result["event_id"],
        initiative="BUR-TEST-001",
        task_id="BUR-TEST-001-T999",
        path=str(plan_path),
    )
    assert plan["plan"]["task_json"]["metadata"]["live_register_event_id"] == result["event_id"]
    with pytest.raises(StateError, match="requires review"):
        apply_live_promote_plan(registry, path=str(plan_path))

    value = json.loads(plan_path.read_text())
    value["review"] = {"required": True, "status": "reviewed", "reviewer": "test"}
    plan_path.write_text(json.dumps(value))
    applied = apply_live_promote_plan(registry, path=str(plan_path))

    assert applied["status"] == "applied"
    assert applied["queue_mutated"] is False
    assert (root / "registry/tasks/BUR-TEST-001-T999.json").is_file()
    queue = json.loads((root / "registry/queue.json").read_text())
    assert "BUR-TEST-001-T999" not in queue["lanes"]["now"]
