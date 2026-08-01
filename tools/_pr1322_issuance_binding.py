from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one anchor, observed {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


v2 = Path("src/bureau/v2.py")
tests = Path("tests/test_v2.py")

replace_once(
    v2,
    '''COORDINATED_CLAIM_MIN_LEASE_SECONDS = 60
''',
    '''COORDINATED_CLAIM_MIN_LEASE_SECONDS = 60
COORDINATED_CLAIM_ISSUED_EVENT = "coordinated-claim-intent-issued"
''',
)

replace_once(
    v2,
    '''def _validate_coordinated_claim_intent(intent: dict[str, Any]) -> None:
''',
    '''def _coordinated_claim_issuance(
    intent: dict[str, Any], action_class: str
) -> dict[str, Any]:
    approval = intent["operator_approval"]
    return {
        "schema_version": 1,
        "run_id": intent["run_id"],
        "task_id": intent["task_id"],
        "worker_id": intent["worker_id"],
        "reviewer": approval["reviewer"],
        "action_class": action_class,
        "approval_level": approval["level"],
        "approval_sha256": legacy.sha256_json(approval),
        "intent_sha256": intent["intent_sha256"],
        "expires_at_unix": intent["expires_at_unix"],
    }


def _validate_coordinated_claim_intent(intent: dict[str, Any]) -> None:
''',
)

replace_once(
    v2,
    '''    def register_worker(self, worker_id: str, kind: str, capabilities: tuple[str, ...]) -> None:
''',
    '''    def issue_claim_intent(
        self, intent: dict[str, Any], action_class: str
    ) -> dict[str, Any]:
        issuance = _coordinated_claim_issuance(intent, action_class)
        with self.immediate() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM events "
                "WHERE run_id=? AND event_type=? ORDER BY event_id",
                (intent["run_id"], COORDINATED_CLAIM_ISSUED_EVENT),
            ).fetchall()
            if len(rows) > 1:
                raise legacy.StateError(
                    "coordinated claim intent has multiple issuance records"
                )
            if rows:
                existing = json.loads(rows[0]["payload_json"])
                if existing != issuance:
                    raise legacy.StateError(
                        "coordinated claim run id already has another issuance"
                    )
                return existing
            self.event(
                connection,
                COORDINATED_CLAIM_ISSUED_EVENT,
                issuance,
                intent["run_id"],
            )
        return issuance

    def claim_intent_issuance(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM events "
                "WHERE run_id=? AND event_type=? ORDER BY event_id",
                (run_id, COORDINATED_CLAIM_ISSUED_EVENT),
            ).fetchall()
        if len(rows) != 1:
            raise legacy.StateError(
                "coordinated claim intent has no unique issuance record"
            )
        issuance = json.loads(rows[0]["payload_json"])
        required = {
            "schema_version",
            "run_id",
            "task_id",
            "worker_id",
            "reviewer",
            "action_class",
            "approval_level",
            "approval_sha256",
            "intent_sha256",
            "expires_at_unix",
        }
        if not isinstance(issuance, dict) or set(issuance) != required:
            raise legacy.StateError(
                "coordinated claim issuance fields are not exact"
            )
        if issuance.get("schema_version") != 1:
            raise legacy.StateError(
                "coordinated claim issuance schema is invalid"
            )
        text_fields = required - {"schema_version", "expires_at_unix"}
        if not all(
            isinstance(issuance.get(field), str) and issuance[field]
            for field in text_fields
        ):
            raise legacy.StateError(
                "coordinated claim issuance identity is incomplete"
            )
        if not isinstance(issuance.get("expires_at_unix"), int):
            raise legacy.StateError(
                "coordinated claim issuance expiry is invalid"
            )
        return issuance

    def register_worker(self, worker_id: str, kind: str, capabilities: tuple[str, ...]) -> None:
''',
)

replace_once(
    v2,
    '''        """Return a source-bound claim plan without mutating Bureau state."""
''',
    '''        """Issue a source-bound plan without claiming or creating run effects."""
''',
)

replace_once(
    v2,
    '''        intent["intent_sha256"] = coordinated_claim_intent_sha256(intent)
        return {
''',
    '''        intent["intent_sha256"] = coordinated_claim_intent_sha256(intent)
        self.store.issue_claim_intent(
            intent, _coordinated_task_action_class(selected)
        )
        return {
''',
)

replace_once(
    v2,
    '''        if intent["expires_at_unix"] <= int(datetime.now(timezone.utc).timestamp()):
            raise legacy.StateError("coordinated claim intent expired")
        runtime_truth = self._runtime_execution_truth()
''',
    '''        if intent["expires_at_unix"] <= int(datetime.now(timezone.utc).timestamp()):
            raise legacy.StateError("coordinated claim intent expired")
        issuance = self.store.claim_intent_issuance(intent["run_id"])
        expected_issuance = _coordinated_claim_issuance(
            intent, issuance["action_class"]
        )
        if issuance != expected_issuance:
            raise legacy.StateError(
                "coordinated claim intent differs from issued identity"
            )
        runtime_truth = self._runtime_execution_truth()
''',
)

replace_once(
    v2,
    '''        approval_action_class = _coordinated_task_action_class(task)
        if approval.reviewer != intent["worker_id"]:
''',
    '''        approval_action_class = _coordinated_task_action_class(task)
        if issuance["action_class"] != approval_action_class:
            raise legacy.StateError(
                "coordinated claim issued action class differs from task"
            )
        if approval.reviewer != intent["worker_id"]:
''',
)

replace_once(
    tests,
    '''def test_coordinated_claim_intent_is_read_only_and_requires_approval(
''',
    '''def test_coordinated_claim_intent_records_issuance_and_requires_approval(
''',
)

replace_once(
    tests,
    '''    assert handoff["minimum_remaining_seconds"] == 60
    assert store.list_runs() == []
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0
''',
    '''    assert handoff["minimum_remaining_seconds"] == 60
    assert store.list_runs() == []
    issuance = store.claim_intent_issuance(result["intent"]["run_id"])
    assert issuance["intent_sha256"] == result["intent"]["intent_sha256"]
    assert issuance["worker_id"] == "operator"
    assert issuance["reviewer"] == "operator"
    assert issuance["action_class"] == "repository_mutation"
    assert issuance["approval_level"] == "operator"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=?",
            (bureau_v2.COORDINATED_CLAIM_ISSUED_EVENT,),
        ).fetchone()[0] == 1
''',
)

replace_once(
    tests,
    '''    intent["operator_approval"]["level"] = "operator"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="approval required for runtime_mutation"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
''',
    '''    intent["operator_approval"]["level"] = "operator"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
''',
)

replace_once(
    tests,
    '''    intent["operator_approval"]["scope"] = []
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="approval scope differs from task action class"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
''',
    '''    intent["operator_approval"]["scope"] = []
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
''',
)

replace_once(
    tests,
    '''    intent["operator_approval"]["reviewer"] = "other-worker"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="approval reviewer differs from worker"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
''',
    '''    intent["operator_approval"]["reviewer"] = "other-worker"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
''',
)

replace_once(
    tests,
    '''def test_coordinated_claim_handoff_does_not_require_absent_lease(
''',
    '''def test_coordinated_runtime_claim_rejects_worker_and_reviewer_transfer(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        break_glass=True,
    )["intent"]
    intent["worker_id"] = "other-worker"
    intent["operator_approval"]["reviewer"] = "other-worker"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    assert store.list_runs() == []


def test_coordinated_claim_handoff_does_not_require_absent_lease(
''',
)

print("Applied durable coordinated claim intent issuance binding.")
