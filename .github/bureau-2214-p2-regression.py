from pathlib import Path


path = Path("tests/test_task_closeout.py")
text = path.read_text(encoding="utf-8")
marker = "\n\ndef test_idempotent_replay_backfills_multiple_historical_tasks_atomically("
if text.count(marker) != 1:
    raise SystemExit("P2 regression insertion marker changed")
test = r'''


def test_idempotent_replay_skips_unrelated_root_mismatch_when_target_is_journaled(
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

    store.set_initiative_state("INIT-A", "active")
    with store.immediate() as connection:
        connection.execute(
            "UPDATE initiative_status SET state='waiting' WHERE initiative_id='INIT-A'"
        )
    store.set_initiative_state("INIT-B", "active")
    with pytest.raises(StateError, match="projection replay root digest mismatch"):
        store.replay_projection()
    with store.connect() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        repair_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='state-projection-repair-v1'"
        ).fetchone()[0]

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
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='state-projection-repair-v1'"
        ).fetchone()[0] == repair_count
    with pytest.raises(StateError, match="projection replay root digest mismatch"):
        store.replay_projection()
'''
path.write_text(text.replace(marker, test + marker, 1), encoding="utf-8")
