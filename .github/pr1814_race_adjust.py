from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


source_path = Path("src/bureau/v2.py")
source = source_path.read_text(encoding="utf-8")
source = replace_once(
    source,
    '''    def set_initiative_state(self, initiative_id: str, state: str) -> dict[str, Any]:
        if not initiative_id or not state:
            raise legacy.StateError("initiative_id and state must be non-empty")
        with self.immediate() as connection:
            existing = connection.execute(
                "SELECT initiative_id,state FROM initiative_status WHERE initiative_id=?",
                (initiative_id,),
            ).fetchone()
            if existing is not None and existing["state"] == state:
                return dict(existing)
            now = legacy.utc_now()
            connection.execute(
                "INSERT INTO initiative_status(initiative_id,state,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(initiative_id) DO UPDATE SET "
                "state=excluded.state,updated_at=excluded.updated_at",
                (initiative_id, state, now),
            )
            self.event(
                connection,
                "initiative-state-set",
                {"initiative_id": initiative_id, "state": state},
                initiative_id=initiative_id,
            )
            row = connection.execute(
                "SELECT initiative_id,state FROM initiative_status WHERE initiative_id=?",
                (initiative_id,),
            ).fetchone()
        return dict(row)''',
    '''    def set_initiative_state(
        self,
        initiative_id: str,
        state: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if not initiative_id or not state:
            raise legacy.StateError("initiative_id and state must be non-empty")
        if connection is None:
            with self.immediate() as write_connection:
                return self.set_initiative_state(
                    initiative_id, state, connection=write_connection
                )
        existing = connection.execute(
            "SELECT initiative_id,state FROM initiative_status WHERE initiative_id=?",
            (initiative_id,),
        ).fetchone()
        if existing is not None and existing["state"] == state:
            return dict(existing)
        now = legacy.utc_now()
        connection.execute(
            "INSERT INTO initiative_status(initiative_id,state,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(initiative_id) DO UPDATE SET "
            "state=excluded.state,updated_at=excluded.updated_at",
            (initiative_id, state, now),
        )
        self.event(
            connection,
            "initiative-state-set",
            {"initiative_id": initiative_id, "state": state},
            initiative_id=initiative_id,
        )
        row = connection.execute(
            "SELECT initiative_id,state FROM initiative_status WHERE initiative_id=?",
            (initiative_id,),
        ).fetchone()
        return dict(row)''',
    "connection-aware initiative state writer",
)
source = replace_once(
    source,
    '''            # Registry-first recovery remains intentional: if the DB write or
            # commit fails, the stale completion-ready row selects this already
            # completed file on retry.  Evidence and the authoritative StateStore
            # completion are nevertheless bound by this one write transaction.
            now = legacy.utc_now()
            connection.execute(
                "INSERT INTO initiative_status(initiative_id,state,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(initiative_id) DO UPDATE SET "
                "state=excluded.state,updated_at=excluded.updated_at",
                (raw["id"], "completed", now),
            )
            store.event(
                connection,
                "initiative-state-set",
                {"initiative_id": raw["id"], "state": "completed"},
                initiative_id=raw["id"],
            )''',
    '''            # Registry-first recovery remains intentional: if the DB write or
            # commit fails, the stale completion-ready row selects this already
            # completed file on retry. Evidence and the authoritative StateStore
            # completion share this one write transaction and canonical writer.
            store.set_initiative_state(
                raw["id"], "completed", connection=connection
            )''',
    "canonical close-ready state writer",
)
source_path.write_text(source, encoding="utf-8")


tests_path = Path("tests/test_v2.py")
tests = tests_path.read_text(encoding="utf-8")
tests = replace_once(
    tests,
    '''    def fail_first_state_store_completion(initiative_id: str, state: str) -> None:
        nonlocal injected
        if not injected and state == "completed":
            injected = True
            raise RuntimeError("injected state-store completion failure")
        original_set_initiative_state(initiative_id, state)''',
    '''    def fail_first_state_store_completion(
        initiative_id: str,
        state: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        nonlocal injected
        if not injected and state == "completed":
            injected = True
            raise RuntimeError("injected state-store completion failure")
        return original_set_initiative_state(
            initiative_id, state, connection=connection
        )''',
    "state-store retry fault injection",
)
tests_path.write_text(tests, encoding="utf-8")
