from __future__ import annotations

import json
import sqlite3

import pytest

from bureau import state_events
from bureau.core import StateError, StateStore


def test_initiative_transition_is_idempotent_and_replayable(tmp_path):
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")

    first = store.set_initiative_state("INIT-1", "active")
    second = store.set_initiative_state("INIT-1", "active")

    assert first == second == {"initiative_id": "INIT-1", "state": "active"}
    with store.connect() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='initiative-state-set'"
        ).fetchone()[0]
        projection_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=?",
            (state_events.PROJECTION_EVENT_TYPE,),
        ).fetchone()[0]
    assert event_count == 1
    assert projection_count == 2  # migration baseline plus one transition delta
    replayed = store.replay_projection()
    assert replayed["matches_current"] is True
    assert replayed["projection"]["initiatives"]["INIT-1"]["state"] == "active"


def test_unknown_event_type_rolls_back_without_event_effect(tmp_path):
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")

    with (
        pytest.raises(StateError, match="unknown Bureau state event type"),
        store.immediate() as connection,
    ):
        store.event(connection, "unknown-outcome", {"state": "unknown"}, "RUN-1")

    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='unknown-outcome'"
        ).fetchone()[0] == 0


def test_projection_failure_rolls_back_projection_and_event(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")

    def fail_delta(*args, **kwargs):
        raise state_events.StateEventError("synthetic projection crash")

    monkeypatch.setattr(state_events, "delta_payload", fail_delta)
    with pytest.raises(StateError, match="synthetic projection crash"):
        store.set_initiative_state("INIT-CRASH", "active")

    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM initiative_status WHERE initiative_id='INIT-CRASH'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='initiative-state-set'"
        ).fetchone()[0] == 0


def test_replay_rejects_unknown_event_schema_version(tmp_path):
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO events(run_id,event_type,event_schema_version,payload_json,created_at) "
            "VALUES(NULL,?,?,?,?)",
            (
                state_events.PROJECTION_EVENT_TYPE,
                2,
                json.dumps({}),
                "2026-08-07T00:00:00Z",
            ),
        )

    with pytest.raises(StateError, match="unsupported state event schema version"):
        store.replay_projection()


def test_replay_rejects_projection_hash_tamper(tmp_path):
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    store.set_initiative_state("INIT-1", "active")
    with store.immediate() as connection:
        row = connection.execute(
            "SELECT event_id,payload_json FROM events WHERE event_type=? "
            "ORDER BY event_id DESC LIMIT 1",
            (state_events.PROJECTION_EVENT_TYPE,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["root_sha256"] = "0" * 64
        connection.execute(
            "UPDATE events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["event_id"]),
        )

    with pytest.raises(StateError, match="projection replay root digest mismatch"):
        store.replay_projection()


def test_schema4_missing_projection_baseline_fails_closed(tmp_path):
    path = tmp_path / "state" / "bureau.sqlite3"
    store = StateStore(path)
    with store.immediate() as connection:
        connection.execute(
            "DELETE FROM events WHERE event_type=?",
            (state_events.PROJECTION_EVENT_TYPE,),
        )

    with pytest.raises(StateError, match="state projection baseline is missing"):
        StateStore(path)


def test_projection_schema_drift_fails_closed():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE events(event_id INTEGER PRIMARY KEY,event_type TEXT,payload_json TEXT)"
    )

    with pytest.raises(state_events.StateEventError, match="state projection schema drift"):
        state_events.current_projection(connection)
