"""Compare-and-swap contract for the internal Bureau close writer.

The close command must bind its expectations — receipt absence, run state and the
claim baseline — inside the same transaction that writes the effect. A concurrent
fail, cancel or baseline drift has to lose deterministically, with a
machine-readable reason and without changing any truth.

The claim baseline is bound to the Git-backed registry documents, not to the
snapshot the caller loaded earlier: ``BEGIN IMMEDIATE`` orders SQLite writers and
cannot order writes to the registry files. A first close therefore reads the task
and plan revision inside the transaction immediately before and after the SQLite
effects. This detects drift between those reads, but not a non-cooperating writer
between the second read and commit. Replay-only filesystem work runs after the
transaction.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import pytest
from test_v2 import setup

import bureau.v2 as bureau_v2
from bureau.core import Registry, RunStateConflict, StateStore, fail_run
from bureau.v2 import _complete_run_after_typed_evaluation as complete_run


def _task_status(store: StateStore, task_id: str) -> str | None:
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM task_status WHERE task_id=?", (task_id,)
        ).fetchone()
    return None if row is None else row["state"]


def _claimed_run(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    return root, registry, store, run


def _task_path(root):
    return next((root / "registry/tasks").glob("*.json"))


def _write_task(root, **changes):
    """Mutate the authoritative task document without touching any loaded snapshot."""
    path = _task_path(root)
    task = json.loads(path.read_text())
    task.update(changes)
    path.write_text(json.dumps(task))
    return task


def _race_before_commit(store: StateStore, action) -> None:
    """Run ``action`` once, immediately before the next CAS section opens."""
    original = store.immediate
    fired: list[bool] = []

    def racing_immediate():
        if not fired:
            fired.append(True)
            action()
        return original()

    store.immediate = racing_immediate  # type: ignore[method-assign]


def _race_inside_transaction(store: StateStore, action) -> None:
    """Run ``action`` once inside the CAS section, after the close has written."""
    original = store.event
    fired: list[bool] = []

    def racing_event(connection, event_type, payload, run_id=None):
        original(connection, event_type, payload, run_id)
        if event_type == "run-completed" and not fired:
            fired.append(True)
            action()

    store.event = racing_event  # type: ignore[method-assign]


def test_close_loses_against_concurrent_cancel(registry_factory, tmp_path, monkeypatch):
    _root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    run_id = run["run_id"]
    _race_before_commit(store, lambda: fail_run(store, run_id, "operator cancelled", "cancelled"))

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, run_id, {"proof": {"result": "passed"}})

    assert error.value.code == "run-not-active"
    payload = error.value.payload()
    assert payload["status"] == "rejected"
    assert payload["effect_applied"] is False
    assert payload["details"]["observed_state"] == "cancelled"
    assert store.run(run_id)["state"] == "cancelled"
    assert store.receipt(run_id) is None
    assert _task_status(store, run["task_id"]) is None


def test_close_loses_against_concurrent_failure(registry_factory, tmp_path, monkeypatch):
    _root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    run_id = run["run_id"]
    _race_before_commit(store, lambda: fail_run(store, run_id, "worker died", "failed"))

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, run_id, {"proof": {"result": "passed"}})

    assert error.value.code == "run-not-active"
    assert store.run(run_id)["state"] == "failed"
    assert store.run(run_id)["error"] == "worker died"
    assert store.receipt(run_id) is None


def test_close_loses_against_baseline_drift_inside_cas(registry_factory, tmp_path, monkeypatch):
    root, _registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    _write_task(root, title="Drift")

    changed = Registry.load(root)
    with pytest.raises(RunStateConflict) as error:
        complete_run(changed, store, run["run_id"], {"proof": {"result": "passed"}})

    assert error.value.code == "stale-baseline"
    details = error.value.payload()["details"]
    assert details["expected_task_sha256"] != details["observed_task_sha256"]
    assert store.receipt(run["run_id"]) is None
    assert store.run(run["run_id"])["state"] == "assigned"


def test_close_rejects_unknown_run_with_machine_readable_reason(
    registry_factory, tmp_path, monkeypatch
):
    _root, registry, store, _run = _claimed_run(registry_factory, tmp_path, monkeypatch)

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, "BUR-RUN-does-not-exist", {"proof": True})

    assert error.value.code == "unknown-run"
    assert error.value.payload()["does_not_establish"] == [
        "safe_retry_without_readback",
        "task_verification",
        "receipt_validity",
    ]


def test_concurrent_closes_produce_exactly_one_effect(registry_factory, tmp_path, monkeypatch):
    """Parallel closes of the same run yield one receipt and explicit idempotent losers."""
    _root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    run_id = run["run_id"]
    start = threading.Barrier(4)
    results: list[dict] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def close() -> None:
        start.wait()
        try:
            value = complete_run(registry, store, run_id, {"proof": {"result": "passed"}})
        except BaseException as exc:
            with lock:
                failures.append(exc)
            return
        with lock:
            results.append(value)

    threads = [threading.Thread(target=close) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    assert not failures, failures
    winners = [item for item in results if item["idempotent"] is False]
    assert len(winners) == 1
    assert len(results) == 4
    receipt_shas = {item["receipt"]["receipt_sha256"] for item in results}
    assert len(receipt_shas) == 1

    with store.connect() as connection:
        receipt_count = connection.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE run_id=?", (run_id,)
        ).fetchone()["n"]
        completed_events = connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE run_id=? AND event_type='run-completed'",
            (run_id,),
        ).fetchone()["n"]
    assert receipt_count == 1
    assert completed_events == 1
    assert store.run(run_id)["state"] == "succeeded"


def test_close_replays_idempotently_after_crash_between_effect_and_receipt_file(
    registry_factory, tmp_path, monkeypatch
):
    """A crash after the committed effect must replay to the same receipt, not a new one."""
    _root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    run_id = run["run_id"]
    first = complete_run(registry, store, run_id, {"proof": {"result": "passed"}})
    store.receipt_path(run_id).unlink()

    replay = complete_run(registry, store, run_id, {})

    assert replay["idempotent"] is True
    assert replay["receipt"]["receipt_sha256"] == first["receipt"]["receipt_sha256"]
    assert store.receipt_path(run_id).exists()
    assert store.run(run_id)["state"] == "succeeded"


def test_idempotent_replay_performs_file_and_registry_work_after_immediate(
    registry_factory, tmp_path, monkeypatch
):
    """Replay keeps one DB transaction but performs both filesystem operations outside it."""
    _root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    run_id = run["run_id"]
    complete_run(registry, store, run_id, {"proof": {"result": "passed"}})
    store.receipt_path(run_id).unlink()

    transaction_active = False
    transactions = 0
    connections = 0
    calls: list[str] = []
    original_immediate = store.immediate
    original_connect = store.connect
    original_materialize = bureau_v2._materialize_receipt
    original_current = bureau_v2._receipt_binds_current_revision

    def counting_connect():
        nonlocal connections
        connections += 1
        return original_connect()

    @contextmanager
    def tracked_immediate():
        nonlocal transaction_active, transactions
        transactions += 1
        transaction_active = True
        try:
            with original_immediate() as connection:
                yield connection
        finally:
            transaction_active = False

    def checked_materialize(target_store, receipt):
        assert transaction_active is False
        calls.append("materialize")
        return original_materialize(target_store, receipt)

    def checked_current(target_registry, receipt):
        assert transaction_active is False
        calls.append("current")
        return original_current(target_registry, receipt)

    store.connect = counting_connect  # type: ignore[method-assign]
    store.immediate = tracked_immediate  # type: ignore[method-assign]
    monkeypatch.setattr(bureau_v2, "_materialize_receipt", checked_materialize)
    monkeypatch.setattr(bureau_v2, "_receipt_binds_current_revision", checked_current)

    replay = complete_run(registry, store, run_id, {})

    assert replay["idempotent"] is True
    assert replay["current"] is True
    assert transactions == 1
    assert connections == 1
    assert calls == ["materialize", "current"]
    assert store.receipt_path(run_id).exists()


def test_close_reads_authoritative_state_in_a_single_transaction(
    registry_factory, tmp_path, monkeypatch
):
    """The close path holds no unsynchronised pre-reads outside its CAS section."""
    _root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    connections = 0
    original_connect = store.connect

    def counting_connect():
        nonlocal connections
        connections += 1
        return original_connect()

    store.connect = counting_connect  # type: ignore[method-assign]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})
    store.connect = original_connect  # type: ignore[method-assign]

    assert connections == 1


def test_first_close_reads_registry_inside_immediate_around_sqlite_effects(
    registry_factory, tmp_path, monkeypatch
):
    """Both authoritative reads stay in BEGIN IMMEDIATE and bracket the effects."""
    _root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    transaction_active = False
    sqlite_effect_started = False
    revision_phases: list[bool] = []
    original_immediate = store.immediate
    original_revision = bureau_v2._authoritative_close_revision

    @contextmanager
    def tracked_immediate():
        nonlocal transaction_active, sqlite_effect_started
        transaction_active = True
        try:
            with original_immediate() as connection:

                def trace(statement):
                    nonlocal sqlite_effect_started
                    normalized = statement.lstrip().upper()
                    if normalized.startswith(
                        ("INSERT INTO RECEIPTS", "INSERT INTO TASK_STATUS", "UPDATE RUNS")
                    ):
                        sqlite_effect_started = True

                connection.set_trace_callback(trace)
                yield connection
        finally:
            transaction_active = False

    def checked_revision(target_registry, task_id, *, run_id):
        assert transaction_active is True
        revision_phases.append(sqlite_effect_started)
        return original_revision(target_registry, task_id, run_id=run_id)

    store.immediate = tracked_immediate  # type: ignore[method-assign]
    monkeypatch.setattr(bureau_v2, "_authoritative_close_revision", checked_revision)

    result = complete_run(
        registry, store, run["run_id"], {"proof": {"result": "passed"}}
    )

    assert result["idempotent"] is False
    assert revision_phases == [False, True]


def test_close_binds_the_authoritative_revision_not_the_loaded_snapshot(
    registry_factory, tmp_path, monkeypatch
):
    """A task file that drifts after ``Registry.load`` loses, even against a matching snapshot."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    snapshot_sha = registry.tasks[run["task_id"]].sha256
    _write_task(root, title="Rewritten after the caller loaded the registry")

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    details = error.value.payload()["details"]
    assert error.value.code == "stale-baseline"
    # The snapshot still agrees with the claim baseline; only the document moved,
    # so a snapshot-based comparison would have accepted this close.
    assert snapshot_sha == run["task_sha256"]
    assert details["observed_task_sha256"] != run["task_sha256"]
    assert store.receipt(run["run_id"]) is None
    assert store.run(run["run_id"])["state"] == "assigned"
    assert _task_status(store, run["task_id"]) is None


def test_close_binds_the_authoritative_plan_revision(registry_factory, tmp_path, monkeypatch):
    """Plan drift is read from the initiative document, not from the loaded snapshot."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {"repository": "bureau", "path": "docs/plan.md"}
    initiative_path.write_text(json.dumps(initiative))

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    details = error.value.payload()["details"]
    assert error.value.code == "stale-baseline"
    assert registry.initiatives["BUR-TEST-001"].current_plan is None
    assert details["observed_plan_sha256"] != run["plan_sha256"]
    assert details["initiative_id"] == "BUR-TEST-001"
    assert store.receipt(run["run_id"]) is None
    assert store.run(run["run_id"])["state"] == "assigned"


def test_close_ignores_stale_snapshot_when_authoritative_revision_matches_run_baseline(
    registry_factory, tmp_path, monkeypatch
):
    """The loaded snapshot adds no safety after both authoritative revision checks."""
    root, _registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    original = _task_path(root).read_text()
    _write_task(root, title="Drift the snapshot was loaded from")
    drifted = Registry.load(root)
    _task_path(root).write_text(original)

    assert drifted.tasks[run["task_id"]].sha256 != run["task_sha256"]
    result = complete_run(
        drifted, store, run["run_id"], {"proof": {"result": "passed"}}
    )

    assert result["idempotent"] is False
    assert result["current"] is True
    assert result["receipt"]["task_sha256"] == run["task_sha256"]
    assert store.run(run["run_id"])["state"] == "succeeded"


def test_close_rejects_when_the_authoritative_document_cannot_be_read(
    registry_factory, tmp_path, monkeypatch
):
    """An unreadable registry revision fails the close closed instead of trusting the snapshot."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    _task_path(root).write_text("{ truncated")

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    details = error.value.payload()["details"]
    assert error.value.code == "registry-revision-unavailable"
    assert details["document_kind"] == "task"
    assert details["matches"] == 0
    assert store.receipt(run["run_id"]) is None
    assert store.run(run["run_id"])["state"] == "assigned"
    assert _task_status(store, run["task_id"]) is None


def test_close_rejects_when_the_authoritative_document_is_missing(
    registry_factory, tmp_path, monkeypatch
):
    """A missing task document cannot fall back to the loaded snapshot."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    _task_path(root).unlink()

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    details = error.value.payload()["details"]
    assert error.value.code == "registry-revision-unavailable"
    assert details["document_kind"] == "task"
    assert details["matches"] == 0
    assert store.receipt(run["run_id"]) is None
    assert store.run(run["run_id"])["state"] == "assigned"


def test_close_rejects_when_the_authoritative_document_is_ambiguous(
    registry_factory, tmp_path, monkeypatch
):
    """Two readable documents with the task id fail closed before any effect."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    task_path = _task_path(root)
    duplicate = root / "registry/tasks/duplicate.json"
    duplicate.write_text(task_path.read_text())

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    details = error.value.payload()["details"]
    assert error.value.code == "registry-revision-unavailable"
    assert details["document_kind"] == "task"
    assert details["matches"] == 2
    assert set(details["match_paths"]) == {str(task_path), str(duplicate)}
    assert store.receipt(run["run_id"]) is None
    assert store.run(run["run_id"])["state"] == "assigned"


def test_close_loses_against_registry_drift_during_the_write(
    registry_factory, tmp_path, monkeypatch
):
    """A revision that moves between binding and commit rolls the whole close back."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    _race_inside_transaction(store, lambda: _write_task(root, title="Drift mid-write"))

    with pytest.raises(RunStateConflict) as error:
        complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    payload = error.value.payload()
    assert error.value.code == "close-revision-drift"
    assert payload["effect_applied"] is False
    assert payload["details"]["expected_task_sha256"] != payload["details"]["observed_task_sha256"]
    assert store.receipt(run["run_id"]) is None
    assert store.run(run["run_id"])["state"] == "assigned"
    assert _task_status(store, run["task_id"]) is None
    with store.connect() as connection:
        events = connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE run_id=? AND event_type='run-completed'",
            (run["run_id"],),
        ).fetchone()["n"]
    assert events == 0


def test_close_accepts_a_rewrite_that_keeps_the_revision(registry_factory, tmp_path, monkeypatch):
    """The close binds the task revision, not the byte encoding of its document."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    task = json.loads(_task_path(root).read_text())
    _task_path(root).write_text(json.dumps(task, indent=2, sort_keys=True) + "\n")

    result = complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    assert result["idempotent"] is False
    assert result["current"] is True
    assert result["receipt"]["task_sha256"] == run["task_sha256"]
    assert result["receipt"]["plan_sha256"] == run["plan_sha256"]
    assert store.run(run["run_id"])["state"] == "succeeded"
    assert _task_status(store, run["task_id"]) == "verified"


def test_close_accepts_the_closure_stamp_the_registry_writes_after_verification(
    registry_factory, tmp_path, monkeypatch
):
    """Task ``state`` and verification metadata stay outside the frozen revision."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    _write_task(
        root,
        state="verified",
        metadata={"verification": {"task_sha256": run["task_sha256"]}},
    )

    result = complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    assert result["current"] is True
    assert store.run(run["run_id"])["state"] == "succeeded"
    assert _task_status(store, run["task_id"]) == "verified"


def test_idempotent_replay_reads_current_from_the_registry_not_the_snapshot(
    registry_factory, tmp_path, monkeypatch
):
    """A replay must not report a receipt as current on the strength of a stale snapshot."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    run_id = run["run_id"]
    first = complete_run(registry, store, run_id, {"proof": {"result": "passed"}})
    assert first["current"] is True
    _write_task(root, title="Changed after verification")

    replay = complete_run(registry, store, run_id, {})

    assert replay["idempotent"] is True
    assert replay["receipt"]["receipt_sha256"] == first["receipt"]["receipt_sha256"]
    assert replay["current"] is False


def test_idempotent_replay_reports_not_current_when_the_revision_is_unreadable(
    registry_factory, tmp_path, monkeypatch
):
    """``current`` is a claim about registry truth, so an unreadable revision is not current."""
    root, registry, store, run = _claimed_run(registry_factory, tmp_path, monkeypatch)
    run_id = run["run_id"]
    complete_run(registry, store, run_id, {"proof": {"result": "passed"}})
    _task_path(root).unlink()

    replay = complete_run(registry, store, run_id, {})

    assert replay["idempotent"] is True
    assert replay["current"] is False
