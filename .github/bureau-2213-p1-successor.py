from pathlib import Path


source_path = Path("src/bureau/task_closeout.py")
source = source_path.read_text(encoding="utf-8")

old_import = "from . import legacy, task_specs\n"
new_import = "from . import legacy, state_events, task_specs\n"
if source.count(old_import) != 1:
    raise SystemExit("task_closeout import preimage changed")
source = source.replace(old_import, new_import, 1)

marker = "\n\ndef apply_task_no_run_closeout(\n"
helper = r'''


def _backfill_replayed_task_projection(
    connection: Any,
    store: StateStore,
    *,
    task_id: str,
) -> bool:
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT event_id,run_id,event_type,event_schema_version,payload_json "
            "FROM events ORDER BY event_id"
        )
    ]
    base_rows, _task_spec_rows = task_specs.split_event_rows(rows)
    try:
        replayed = state_events.replay(base_rows)["projection"]
        current = state_events.current_projection(connection)
        diff = state_events.projection_diff(replayed, current)
    except state_events.StateEventError as exc:
        raise legacy.StateError(str(exc)) from exc
    if task_id not in diff["tasks"]:
        return False
    if any(
        key != "tasks" or entity_id != task_id
        for key, values in diff.items()
        for entity_id in values
    ):
        raise legacy.StateError(
            "task no-run closeout replay refuses unrelated StateStore drift"
        )
    store.append_task_projection_delta(
        connection,
        trigger="task-no-run-closeout-replay",
        task_id=task_id,
    )
    return True
'''
if source.count(marker) != 1:
    raise SystemExit("apply marker changed")
source = source.replace(marker, helper + marker, 1)

old_replay = '''        replay = _validated_replay(preview, expected_preview_sha256)
        if replay is not None:
            return replay
'''
new_replay = '''        replay = _validated_replay(preview, expected_preview_sha256)
        if replay is not None:
            _backfill_replayed_task_projection(connection, store, task_id=task_id)
            return replay
'''
if source.count(old_replay) != 1:
    raise SystemExit("replay preimage changed")
source = source.replace(old_replay, new_replay, 1)
source_path.write_text(source, encoding="utf-8")

test_path = Path("tests/test_task_closeout.py")
tests = test_path.read_text(encoding="utf-8")
test_marker = "\n\ndef test_apply_is_idempotent_for_same_verified_receipt("
additions = r'''


def test_idempotent_replay_backfills_historical_task_projection(
    registry_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    real_append = store.append_task_projection_delta
    monkeypatch.setattr(
        store, "append_task_projection_delta", lambda *args, **kwargs: None
    )
    first = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert first["idempotent"] is False
    with pytest.raises(StateError, match="replayed state projection does not match"):
        store.replay_projection()

    monkeypatch.setattr(store, "append_task_projection_delta", real_append)
    second = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert second["idempotent"] is True
    assert store.replay_projection()["matches_current"] is True

    with store.connect() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    third = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert third["idempotent"] is True
    with store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            == event_count
        )


def test_idempotent_replay_refuses_unrelated_drift_while_backfilling(
    registry_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    real_append = store.append_task_projection_delta
    monkeypatch.setattr(
        store, "append_task_projection_delta", lambda *args, **kwargs: None
    )
    apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    monkeypatch.setattr(store, "append_task_projection_delta", real_append)
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO task_status("
            "task_id,state,receipt_sha256,updated_at,task_sha256,plan_sha256"
            ") VALUES(?,?,?,?,?,?)",
            ("UNRELATED-TASK", "verified", "a" * 64, NOW, "b" * 64, "c" * 64),
        )

    with pytest.raises(StateError, match="refuses unrelated StateStore drift"):
        apply_task_no_run_closeout(
            registry,
            store,
            task_id,
            evidence,
            reviewer=REVIEWER,
            expected_preview_sha256=preview["preview_sha256"],
            now=NOW,
        )


def test_idempotent_replay_ignores_unrelated_drift_when_target_is_journaled(
    registry_factory, tmp_path: Path
) -> None:
    _, registry, store, task_id = _setup(registry_factory, tmp_path)
    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    first = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert first["idempotent"] is False
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO task_status("
            "task_id,state,receipt_sha256,updated_at,task_sha256,plan_sha256"
            ") VALUES(?,?,?,?,?,?)",
            ("UNRELATED-TASK", "verified", "a" * 64, NOW, "b" * 64, "c" * 64),
        )
    with store.connect() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    second = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert second["idempotent"] is True
    with store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            == event_count
        )
'''
if tests.count(test_marker) != 1:
    raise SystemExit("test insertion marker changed")
tests = tests.replace(test_marker, additions + test_marker, 1)
test_path.write_text(tests, encoding="utf-8")
