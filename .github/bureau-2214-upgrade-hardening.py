from pathlib import Path


source_path = Path("src/bureau/task_closeout.py")
source = source_path.read_text(encoding="utf-8")
start_marker = "\ndef _backfill_replayed_task_projection(\n"
end_marker = "\ndef apply_task_no_run_closeout(\n"
if source.count(start_marker) != 1 or source.count(end_marker) != 1:
    raise SystemExit("task closeout helper markers changed")
start = source.index(start_marker)
end = source.index(end_marker, start)
helpers = r'''
def _projection_event_rows(connection: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT event_id,run_id,event_type,event_schema_version,payload_json "
            "FROM events ORDER BY event_id"
        )
    ]


def _historical_no_run_task_projections(
    registry: Any,
    connection: Any,
    diff: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if any(diff.get(key) for key in state_events.PROJECTION_KEYS if key != "tasks"):
        raise legacy.StateError(
            "task no-run closeout replay refuses unrelated StateStore drift"
        )
    task_diff = diff.get("tasks")
    if not isinstance(task_diff, Mapping) or not task_diff:
        raise legacy.StateError(
            "task no-run closeout replay has no historical task projection drift"
        )
    effective, _authority, _revisions = _authoritative_task_registry_from_connection(
        registry, connection
    )
    projections: dict[str, dict[str, Any]] = {}
    for changed_task_id, change in task_diff.items():
        if (
            not isinstance(changed_task_id, str)
            or not isinstance(change, Mapping)
            or change.get("replayed") is not None
            or not isinstance(change.get("current"), Mapping)
        ):
            raise legacy.StateError(
                "task no-run closeout replay refuses unrelated StateStore drift"
            )
        task = effective.tasks.get(changed_task_id)
        if task is None or task.state != "verified":
            raise legacy.StateError(
                "task no-run closeout replay refuses unrelated StateStore drift"
            )
        verification = task.raw.get("metadata", {}).get("verification")
        if not isinstance(verification, Mapping) or verification.get("kind") != VERIFICATION_KIND:
            raise legacy.StateError(
                "task no-run closeout replay refuses unrelated StateStore drift"
            )
        receipt = verification.get("receipt")
        if not isinstance(receipt, Mapping):
            raise legacy.StateError(
                "task no-run closeout replay refuses unrelated StateStore drift"
            )
        receipt_sha256 = receipt.get("receipt_sha256")
        unsigned_receipt = {
            key: value for key, value in receipt.items() if key != "receipt_sha256"
        }
        stable_task_sha256 = task_revision_sha256(task.raw)
        current_plan_sha256 = plan_sha256(effective, task.initiative)
        expected_projection = {
            "task_id": changed_task_id,
            "task_sha256": stable_task_sha256,
            "plan_sha256": current_plan_sha256,
            "state": "verified",
            "receipt_sha256": receipt_sha256,
        }
        if (
            not isinstance(receipt_sha256, str)
            or legacy.sha256_json(unsigned_receipt) != receipt_sha256
            or verification.get("receipt_sha256") != receipt_sha256
            or verification.get("task_sha256") != stable_task_sha256
            or verification.get("plan_sha256") != current_plan_sha256
            or receipt.get("kind") != RECEIPT_KIND
            or receipt.get("status") != "verified"
            or receipt.get("task_id") != changed_task_id
            or receipt.get("task_sha256") != stable_task_sha256
            or receipt.get("plan_sha256") != current_plan_sha256
            or dict(change["current"]) != expected_projection
            or connection.execute(
                "SELECT 1 FROM runs WHERE task_id=? LIMIT 1", (changed_task_id,)
            ).fetchone()
            is not None
        ):
            raise legacy.StateError(
                "task no-run closeout replay refuses unrelated StateStore drift"
            )
        projections[changed_task_id] = expected_projection
    return projections


def _append_historical_no_run_projection_delta(
    connection: Any,
    store: StateStore,
    *,
    current: Mapping[str, Any],
    task_projections: Mapping[str, Mapping[str, Any]],
) -> None:
    changes: dict[str, dict[str, Any]] = {
        key: {} for key in state_events.PROJECTION_KEYS
    }
    changes["tasks"] = {
        task_id: dict(task_projections[task_id]) for task_id in sorted(task_projections)
    }
    payload = {
        "schema_version": state_events.EVENT_SCHEMA_VERSION,
        "mode": "delta",
        "trigger": "task-no-run-closeout-replay",
        "changes": changes,
        "root_sha256": state_events.projection_root(current),
    }
    store._insert_event(connection, state_events.PROJECTION_EVENT_TYPE, payload, None)


def _append_historical_no_run_projection_repair(
    connection: Any,
    store: StateStore,
    *,
    assessment: Mapping[str, Any],
    task_id: str,
    reviewer: str,
    expected_preview_sha256: str,
) -> None:
    candidate = assessment.get("candidate")
    candidate_sha256 = assessment.get("candidate_sha256")
    if not isinstance(candidate, Mapping) or not isinstance(candidate_sha256, str):
        raise legacy.StateError("task no-run closeout projection repair assessment is invalid")
    payload = {
        "schema_version": state_events.EVENT_SCHEMA_VERSION,
        "kind": state_events.PROJECTION_REPAIR_KIND,
        "candidate": dict(candidate),
        "candidate_sha256": candidate_sha256,
        "authority": {
            "schema_version": state_events.EVENT_SCHEMA_VERSION,
            "kind": "operator",
            "reviewer": reviewer,
            "reference": f"task-no-run-closeout:{task_id}:{expected_preview_sha256}",
            "reason": "backfill historical verified no-run closeout projection",
        },
    }
    store._insert_event(
        connection, state_events.PROJECTION_REPAIR_EVENT_TYPE, payload, None
    )


def _verify_replayed_projection_matches_current(connection: Any) -> None:
    rows = _projection_event_rows(connection)
    base_rows, _task_spec_rows = task_specs.split_event_rows(rows)
    try:
        replayed = state_events.replay(base_rows)
        current = state_events.current_projection(connection)
    except state_events.StateEventError as exc:
        raise legacy.StateError(str(exc)) from exc
    if replayed["root_sha256"] != state_events.projection_root(current):
        raise legacy.StateError(
            "task no-run closeout replay backfill did not restore StateStore projection"
        )


def _backfill_replayed_task_projection(
    registry: Any,
    connection: Any,
    store: StateStore,
    *,
    task_id: str,
    reviewer: str,
    expected_preview_sha256: str,
) -> bool:
    rows = _projection_event_rows(connection)
    base_rows, _task_spec_rows = task_specs.split_event_rows(rows)
    try:
        current = state_events.current_projection(connection)
        replayed = state_events.replay(base_rows)["projection"]
    except state_events.StateEventError as exc:
        if str(exc) != "projection replay root digest mismatch":
            raise legacy.StateError(str(exc)) from exc
        try:
            current = state_events.current_projection(connection)
            assessment = state_events.projection_repair_candidate(base_rows, current)
        except state_events.StateEventError as repair_exc:
            raise legacy.StateError(str(repair_exc)) from repair_exc
        diff = assessment["candidate"]["diff"]
        if task_id not in diff["tasks"]:
            return False
        _historical_no_run_task_projections(registry, connection, diff)
        _append_historical_no_run_projection_repair(
            connection,
            store,
            assessment=assessment,
            task_id=task_id,
            reviewer=reviewer,
            expected_preview_sha256=expected_preview_sha256,
        )
    else:
        try:
            diff = state_events.projection_diff(replayed, current)
        except state_events.StateEventError as exc:
            raise legacy.StateError(str(exc)) from exc
        if task_id not in diff["tasks"]:
            return False
        task_projections = _historical_no_run_task_projections(
            registry, connection, diff
        )
        _append_historical_no_run_projection_delta(
            connection,
            store,
            current=current,
            task_projections=task_projections,
        )
    _verify_replayed_projection_matches_current(connection)
    return True


'''
source = source[:start] + "\n" + helpers + source[end + 1 :]
old_call = '''            _backfill_replayed_task_projection(connection, store, task_id=task_id)\n'''
new_call = '''            _backfill_replayed_task_projection(\n                registry,\n                connection,\n                store,\n                task_id=task_id,\n                reviewer=reviewer_value,\n                expected_preview_sha256=expected_preview_sha256,\n            )\n'''
if source.count(old_call) != 1:
    raise SystemExit("task closeout replay call changed")
source = source.replace(old_call, new_call, 1)
source_path.write_text(source, encoding="utf-8")

test_path = Path("tests/test_task_closeout.py")
tests = test_path.read_text(encoding="utf-8")
insert_marker = "\n\ndef test_apply_is_idempotent_for_same_verified_receipt("
if tests.count(insert_marker) != 1:
    raise SystemExit("task closeout test insertion marker changed")
extra_tests = r'''


def test_idempotent_replay_backfills_multiple_historical_tasks_atomically(
    registry_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = registry_factory(task_count=2)
    registry = Registry.load(root)
    state_root = tmp_path / "multi-state"
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    task_ids = sorted(registry.tasks)
    with store.immediate() as connection:
        for task_id in task_ids:
            task_specs.put(
                connection,
                registry.tasks[task_id].raw,
                idempotency_key=f"seed-no-run-closeout-{task_id}",
                expected_revision=None,
                source="test-seed",
            )
    real_append = store.append_task_projection_delta
    monkeypatch.setattr(
        store, "append_task_projection_delta", lambda *args, **kwargs: None
    )
    for task_id in task_ids:
        evidence = _evidence_path(tmp_path, registry, store, task_id)
        preview = preview_task_no_run_closeout(
            registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
        )
        receipt = apply_task_no_run_closeout(
            registry,
            store,
            task_id,
            evidence,
            reviewer=REVIEWER,
            expected_preview_sha256=preview["preview_sha256"],
            now=NOW,
        )
        assert receipt["idempotent"] is False
    monkeypatch.setattr(store, "append_task_projection_delta", real_append)
    with pytest.raises(StateError, match="replayed state projection does not match"):
        store.replay_projection()

    target = task_ids[0]
    evidence = _evidence_path(tmp_path, registry, store, target)
    preview = preview_task_no_run_closeout(
        registry, store, target, evidence, reviewer=REVIEWER, now=NOW
    )
    with store.connect() as connection:
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    receipt = apply_task_no_run_closeout(
        registry,
        store,
        target,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert receipt["idempotent"] is True
    assert store.replay_projection()["matches_current"] is True
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before + 1

    other = task_ids[1]
    evidence = _evidence_path(tmp_path, registry, store, other)
    preview = preview_task_no_run_closeout(
        registry, store, other, evidence, reviewer=REVIEWER, now=NOW
    )
    with store.connect() as connection:
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    receipt = apply_task_no_run_closeout(
        registry,
        store,
        other,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert receipt["idempotent"] is True
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_idempotent_replay_repairs_later_root_mismatch(
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
            "INSERT INTO initiative_status(initiative_id,state,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(initiative_id) DO UPDATE SET "
            "state=excluded.state,updated_at=excluded.updated_at",
            ("BUR-TEST-001", "active", NOW),
        )
        store.event(
            connection,
            "initiative-state-set",
            {"initiative_id": "BUR-TEST-001", "state": "active"},
            initiative_id="BUR-TEST-001",
        )
    with pytest.raises(StateError, match="projection replay root digest mismatch"):
        store.replay_projection()

    evidence = _evidence_path(tmp_path, registry, store, task_id)
    preview = preview_task_no_run_closeout(
        registry, store, task_id, evidence, reviewer=REVIEWER, now=NOW
    )
    receipt = apply_task_no_run_closeout(
        registry,
        store,
        task_id,
        evidence,
        reviewer=REVIEWER,
        expected_preview_sha256=preview["preview_sha256"],
        now=NOW,
    )
    assert receipt["idempotent"] is True
    replay = store.replay_projection()
    assert replay["matches_current"] is True
    assert replay["repair_checkpoint_count"] == 1
'''
tests = tests.replace(insert_marker, extra_tests + insert_marker, 1)
test_path.write_text(tests, encoding="utf-8")
