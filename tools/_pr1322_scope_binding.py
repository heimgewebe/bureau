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
    '''        approval = _coordinated_approval_from_dict(approval_data)
        approval_action_class = _coordinated_task_action_class(task)
        approval_decision = require_approval(
''',
    '''        approval = _coordinated_approval_from_dict(approval_data)
        approval_action_class = _coordinated_task_action_class(task)
        if approval.reviewer != intent["worker_id"]:
            raise legacy.StateError(
                "coordinated claim approval reviewer differs from worker"
            )
        if tuple(approval.scope) != (approval_action_class,):
            raise legacy.StateError(
                "coordinated claim approval scope differs from task action class"
            )
        approval_decision = require_approval(
''',
)

replace_once(
    tests,
    '''def test_coordinated_claim_handoff_does_not_require_absent_lease(
''',
    '''def test_coordinated_runtime_claim_rejects_rehashed_scope_widening(
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
    intent["operator_approval"]["scope"] = []
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="approval scope differs from task action class"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    assert store.list_runs() == []


def test_coordinated_runtime_claim_rejects_rehashed_reviewer_drift(
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
    intent["operator_approval"]["reviewer"] = "other-worker"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="approval reviewer differs from worker"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    assert store.list_runs() == []


def test_coordinated_claim_handoff_does_not_require_absent_lease(
''',
)

print("Applied exact coordinated approval scope and reviewer binding.")
