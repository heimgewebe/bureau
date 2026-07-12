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


def test_candidate_supersession_projects_latest_and_preserves_history(
    registry_factory, tmp_path,
):
    _root, registry, store = setup_live(registry_factory, tmp_path)
    first = live_register_record(
        registry,
        store,
        kind="candidate_task",
        repo="repo.alpha",
        title="Original candidate",
        source="chat",
        promotion_required=True,
    )

    corrected = live_register_record(
        registry,
        store,
        kind="candidate_task",
        title="Corrected candidate",
        source="chat",
        supersedes_event_id=first["event_id"],
    )

    assert first["record"]["candidate_id"].startswith("candidate-")
    assert corrected["record"]["candidate_id"] == first["record"]["candidate_id"]
    assert corrected["record"]["supersedes_event_id"] == first["event_id"]
    assert corrected["record"]["repo"] == "repo.alpha"
    assert corrected["record"]["promotion_required"] is True

    listed = live_register_list(store, kind="candidate_task")
    assert len(listed["records"]) == 2
    assert listed["summary"]["candidate_history_count"] == 2
    assert listed["summary"]["superseded_candidate_event_count"] == 1
    assert listed["summary"]["open_candidate_count"] == 1
    assert listed["summary"]["open_candidates"][0]["event_id"] == corrected["event_id"]
    assert listed["summary"]["open_candidates"][0]["record"]["title"] == "Corrected candidate"


def test_candidate_close_event_removes_candidate_from_open_projection(
    registry_factory, tmp_path,
):
    _root, registry, store = setup_live(registry_factory, tmp_path)
    first = live_register_record(
        registry,
        store,
        kind="candidate_task",
        repo="repo.alpha",
        title="Candidate to close",
        promotion_required=True,
    )
    closed = live_register_record(
        registry,
        store,
        kind="candidate_task",
        repo="repo.alpha",
        title="Candidate closed after review",
        status="closed",
        supersedes_event_id=first["event_id"],
    )

    corrected_closed = live_register_record(
        registry,
        store,
        kind="candidate_task",
        title="Closed candidate typo corrected",
        supersedes_event_id=closed["event_id"],
    )
    assert corrected_closed["record"]["status"] == "closed"

    listed = live_register_list(store, kind="candidate_task")
    assert listed["summary"]["open_candidate_count"] == 0
    assert listed["summary"]["promotion_required_count"] == 0
    assert (
        listed["summary"]["latest_candidates"][0]["event_id"]
        == corrected_closed["event_id"]
    )
    assert listed["summary"]["latest_candidates"][0]["record"]["status"] == "closed"

    from bureau.live_register import write_live_promote_plan

    with pytest.raises(StateError, match="only an open candidate_task"):
        write_live_promote_plan(
            registry,
            store,
            event_id=corrected_closed["event_id"],
            initiative="BUR-TEST-001",
            task_id="BUR-TEST-001-T997",
            path=str(tmp_path / "closed-plan.json"),
        )


def test_candidate_supersession_validation_fails_closed(registry_factory, tmp_path):
    _root, registry, store = setup_live(registry_factory, tmp_path)
    focus = live_register_record(
        registry,
        store,
        kind="thread_focus",
        thread_id="chat-focus",
        repo="repo.alpha",
        title="Not a candidate",
    )
    first = live_register_record(
        registry,
        store,
        kind="candidate_task",
        candidate_id="candidate.alpha",
        repo="repo.alpha",
        title="Candidate alpha",
    )

    with pytest.raises(StateError, match="only valid for candidate_task"):
        live_register_record(
            registry,
            store,
            kind="thread_focus",
            thread_id="chat-invalid",
            repo="repo.alpha",
            title="Invalid candidate identity",
            candidate_id="candidate.invalid",
        )
    with pytest.raises(StateError, match="must reference a candidate_task"):
        live_register_record(
            registry,
            store,
            kind="candidate_task",
            repo="repo.alpha",
            title="Wrong predecessor kind",
            supersedes_event_id=focus["event_id"],
        )
    with pytest.raises(StateError, match="existing candidate_id requires"):
        live_register_record(
            registry,
            store,
            kind="candidate_task",
            candidate_id="candidate.alpha",
            repo="repo.alpha",
            title="Duplicate identity without predecessor",
        )
    with pytest.raises(StateError, match="must match the superseded"):
        live_register_record(
            registry,
            store,
            kind="candidate_task",
            candidate_id="candidate.other",
            repo="repo.alpha",
            title="Mismatched correction",
            supersedes_event_id=first["event_id"],
        )
    with pytest.raises(StateError, match="repo cannot change"):
        live_register_record(
            registry,
            store,
            kind="candidate_task",
            repo="repo.beta",
            title="Cross-repository correction",
            supersedes_event_id=first["event_id"],
        )

    corrected = live_register_record(
        registry,
        store,
        kind="candidate_task",
        candidate_id="candidate.alpha",
        repo="repo.alpha",
        title="Valid correction",
        supersedes_event_id=first["event_id"],
    )
    assert corrected["record"]["candidate_id"] == "candidate.alpha"

    with pytest.raises(StateError, match="already superseded"):
        live_register_record(
            registry,
            store,
            kind="candidate_task",
            repo="repo.alpha",
            title="Competing correction",
            supersedes_event_id=first["event_id"],
        )


def test_candidate_supersession_supports_legacy_event_identity(registry_factory, tmp_path):
    _root, registry, store = setup_live(registry_factory, tmp_path)
    legacy_payload = {
        "schema_version": 1,
        "kind": "candidate_task",
        "title": "Legacy candidate",
        "source": "legacy-test",
        "status": "observed",
        "promotion_required": True,
        "does_not_establish": ["queue_truth"],
    }
    with store.immediate() as connection:
        cursor = connection.execute(
            "INSERT INTO events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (None, "live-register", json.dumps(legacy_payload), "2026-07-10T00:00:00Z"),
        )
        legacy_event_id = int(cursor.lastrowid)

    corrected = live_register_record(
        registry,
        store,
        kind="candidate_task",
        title="Legacy candidate corrected",
        supersedes_event_id=legacy_event_id,
    )

    assert corrected["record"]["candidate_id"] == f"candidate-event-{legacy_event_id}"
    listed = live_register_list(store, kind="candidate_task")
    assert listed["summary"]["open_candidate_count"] == 1
    assert listed["summary"]["open_candidates"][0]["event_id"] == corrected["event_id"]


def test_candidate_supersession_reports_missing_legacy_status(
    registry_factory, tmp_path,
):
    _root, registry, store = setup_live(registry_factory, tmp_path)
    malformed_payload = {
        "schema_version": 1,
        "kind": "candidate_task",
        "title": "Malformed legacy candidate",
        "source": "legacy-test",
        "promotion_required": True,
        "does_not_establish": ["queue_truth"],
    }
    with store.immediate() as connection:
        cursor = connection.execute(
            "INSERT INTO events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (
                None,
                "live-register",
                json.dumps(malformed_payload),
                "2026-07-10T00:00:00Z",
            ),
        )
        event_id = int(cursor.lastrowid)

    with pytest.raises(
        StateError, match=rf"candidate event {event_id} is missing required status"
    ):
        live_register_record(
            registry,
            store,
            kind="candidate_task",
            title="Attempted correction",
            supersedes_event_id=event_id,
        )

    listed = live_register_list(store, kind="candidate_task")
    assert listed["summary"]["candidate_history_count"] == 1
    assert listed["records"][0]["event_id"] == event_id


def test_live_promote_plan_rejects_superseded_candidate_event(
    registry_factory, tmp_path,
):
    _root, registry, store = setup_live(registry_factory, tmp_path)
    first = live_register_record(
        registry,
        store,
        kind="candidate_task",
        repo="repo.alpha",
        title="Stale promotion candidate",
    )
    latest = live_register_record(
        registry,
        store,
        kind="candidate_task",
        repo="repo.alpha",
        title="Current promotion candidate",
        supersedes_event_id=first["event_id"],
    )

    from bureau.live_register import write_live_promote_plan

    with pytest.raises(StateError, match="superseded candidate_task"):
        write_live_promote_plan(
            registry,
            store,
            event_id=first["event_id"],
            initiative="BUR-TEST-001",
            task_id="BUR-TEST-001-T998",
            path=str(tmp_path / "stale-plan.json"),
        )

    current = write_live_promote_plan(
        registry,
        store,
        event_id=latest["event_id"],
        initiative="BUR-TEST-001",
        task_id="BUR-TEST-001-T999",
        path=str(tmp_path / "current-plan.json"),
    )
    assert (
        current["plan"]["task_json"]["metadata"]["live_register_candidate_id"]
        == latest["record"]["candidate_id"]
    )


def test_live_register_cli_corrects_and_closes_candidate(
    registry_factory, tmp_path, capsys,
):
    root, _registry, _store = setup_live(registry_factory, tmp_path)
    state_root = tmp_path / "candidate-cli-state"

    rc = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(state_root),
            "--json",
            "live-register",
            "--kind",
            "candidate_task",
            "--candidate-id",
            "candidate.cli",
            "--repo",
            "repo.alpha",
            "--title",
            "CLI candidate",
            "--promotion-required",
        ]
    )
    assert rc == 0
    first = json.loads(capsys.readouterr().out)

    rc = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(state_root),
            "--json",
            "live-register",
            "--kind",
            "candidate_task",
            "--title",
            "CLI candidate closed",
            "--status",
            "closed",
            "--no-promotion-required",
            "--supersedes-event-id",
            str(first["event_id"]),
        ]
    )
    assert rc == 0
    closed = json.loads(capsys.readouterr().out)
    assert closed["record"]["candidate_id"] == "candidate.cli"
    assert closed["record"]["repo"] == "repo.alpha"
    assert closed["record"]["promotion_required"] is False

    rc = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(state_root),
            "--json",
            "live-list",
            "--kind",
            "candidate_task",
        ]
    )
    assert rc == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["summary"]["candidate_history_count"] == 2
    assert listed["summary"]["open_candidate_count"] == 0


def test_live_promote_plan_uses_full_candidate_history_beyond_list_limit(
    registry_factory, tmp_path,
):
    _root, registry, store = setup_live(registry_factory, tmp_path)
    candidate = live_register_record(
        registry,
        store,
        kind="candidate_task",
        repo="repo.alpha",
        title="Old but current candidate",
    )
    filler = {
        "schema_version": 1,
        "kind": "thread_focus",
        "title": "Filler",
        "source": "test",
        "status": "closed",
        "promotion_required": False,
        "thread_id": "filler",
        "repo": "repo.alpha",
        "does_not_establish": ["queue_truth"],
    }
    with store.immediate() as connection:
        connection.executemany(
            "INSERT INTO events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            [
                (
                    None,
                    "live-register",
                    json.dumps({**filler, "thread_id": f"filler-{index}"}),
                    "2026-07-10T00:00:00Z",
                )
                for index in range(501)
            ],
        )

    from bureau.live_register import write_live_promote_plan

    plan = write_live_promote_plan(
        registry,
        store,
        event_id=candidate["event_id"],
        initiative="BUR-TEST-001",
        task_id="BUR-TEST-001-T996",
        path=str(tmp_path / "old-current-plan.json"),
    )
    assert plan["plan"]["event_id"] == candidate["event_id"]


def test_live_projection_remains_complete_beyond_history_limit(
    registry_factory, tmp_path
):
    _root, registry, store = setup_live(registry_factory, tmp_path)
    old = live_register_record(
        registry,
        store,
        kind="thread_focus",
        thread_id="old-still-active",
        repo="repo.alpha",
        title="Old focus remains active",
    )
    for index in range(120):
        live_register_record(
            registry,
            store,
            kind="thread_focus",
            thread_id=f"noise-{index:03d}",
            repo="repo.beta",
            title=f"Noise {index:03d}",
        )

    from bureau.live_register import (
        live_register_conflict_report,
        live_register_context,
        live_register_repo_context,
    )

    listed = live_register_list(store, repo="repo.alpha", limit=50)
    context = live_register_context(store, repo="repo.alpha", limit=50)
    repo_context = live_register_repo_context(store, "repo.alpha", limit=50)
    conflicts = live_register_conflict_report(
        registry, store, repo="repo.alpha", limit=50
    )

    for value in (listed, context, conflicts):
        assert value["coverage_complete"] is True
        assert value["history_truncated"] is True
        assert value["oldest_loaded_event_id"] > old["event_id"]
        assert value["projection_source"] == "complete_event_scan"
    assert listed["records"] == []
    assert listed["summary"]["history_loaded_records"] == 50
    assert listed["summary"]["history_total_records"] == 121
    assert listed["summary"]["active_thread_focus"][0]["event_id"] == old["event_id"]
    assert context["summary"]["active_thread_focus"][0]["event_id"] == old["event_id"]
    assert repo_context["active_thread_focus"][0]["event_id"] == old["event_id"]
    conflict_focus = conflicts["live_register"]["summary"]["active_thread_focus"]
    assert conflict_focus[0]["event_id"] == old["event_id"]
    assert conflicts["summary"]["live_records"] == 0
    assert conflicts["summary"]["history_loaded_records"] == 50
    assert conflicts["summary"]["projection_records"] == 1
    assert conflicts["summary"]["projection_total_records"] == 121


def test_live_projection_uses_one_complete_snapshot(
    registry_factory, tmp_path, monkeypatch
):
    _root, _registry, store = setup_live(registry_factory, tmp_path)

    from bureau import live_register as live_register_module

    monkeypatch.setattr(
        live_register_module,
        "_load_live_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("projection must not read a second history snapshot")
        ),
    )

    report = live_register_module.live_register_list(store, limit=50)

    assert report["coverage_complete"] is True
    assert report["summary"]["history_total_records"] == 0



def test_live_conflicts_fail_closed_when_projection_coverage_is_incomplete(
    registry_factory, tmp_path, monkeypatch
):
    _root, registry, store = setup_live(registry_factory, tmp_path)

    from bureau import live_register as live_register_module

    monkeypatch.setattr(
        live_register_module,
        "_load_live_projection_records",
        lambda _store: (
            [],
            {
                "coverage_complete": False,
                "projection_source": "test-incomplete",
                "projection_records": 0,
            },
        ),
    )

    report = live_register_module.live_register_conflict_report(registry, store)

    assert report["coverage_complete"] is False
    assert report["summary"]["blockers"] == 1
    assert report["findings"][0]["code"] == "live-register-projection-incomplete"
