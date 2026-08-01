from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement anchor, observed {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def insert_before(path: Path, anchor: str, addition: str) -> None:
    replace_once(path, anchor, addition + anchor)


v2 = Path("src/bureau/v2.py")
cli = Path("src/bureau/cli.py")
tests = Path("tests/test_v2.py")

replace_once(
    v2,
    "from .approval import explicit_operator_approval, require_approval, task_approval_contract\n",
    '''from .approval import (
    ApprovalEvidence,
    break_glass_approval,
    explicit_operator_approval,
    require_approval,
    task_approval_contract,
)
''',
)

insert_before(
    v2,
    "\ndef _validate_coordinated_claim_intent(intent: dict[str, Any]) -> None:\n",
    r'''


def _coordinated_task_action_class(task: legacy.Task) -> str:
    contract = task_approval_contract(task.raw)
    action_class = contract.get("action_class")
    if not isinstance(action_class, str) or not action_class:
        raise legacy.StateError(
            f"task {task.id} has no executable approval action class"
        )
    return action_class


def _coordinated_claim_approval(
    task: legacy.Task,
    *,
    worker_id: str,
    run_id: str,
    approved: bool,
    break_glass: bool,
    source: str,
) -> tuple[ApprovalEvidence, dict[str, Any]]:
    if approved and break_glass:
        raise legacy.StateError(
            "coordinated claim approval must choose operator or break-glass authority"
        )
    action_class = _coordinated_task_action_class(task)
    if break_glass:
        approval = break_glass_approval(
            source=source,
            approved=True,
            reviewer=worker_id,
            reference=run_id,
            task_id=task.id,
            scope=action_class,
            note="explicit coordinated claim break-glass approval",
        )
    else:
        approval = explicit_operator_approval(
            source=source,
            approved=approved,
            reviewer=worker_id,
            reference=run_id,
            task_id=task.id,
            scope=action_class,
        )
    decision = require_approval(
        action_class,
        approval,
        expected_reference=run_id,
        task_id=task.id,
    )
    return approval, decision


def _coordinated_approval_from_dict(data: dict[str, Any]) -> ApprovalEvidence:
    if data.get("schema_version") != 1:
        raise legacy.StateError("coordinated claim approval schema is invalid")
    level = data.get("level")
    source = data.get("source")
    reviewer = data.get("reviewer")
    reference = data.get("reference")
    task_id = data.get("task_id")
    if not all(
        isinstance(value, str) and value
        for value in (source, reviewer, reference, task_id)
    ):
        raise legacy.StateError("coordinated claim approval identity is incomplete")
    common = {
        "source": source,
        "approved": data.get("approved") is True,
        "reviewer": reviewer,
        "reference": reference,
        "task_id": task_id,
        "scope": data.get("scope"),
        "note": data.get("note"),
    }
    if level == "operator":
        return explicit_operator_approval(**common)
    if level == "break_glass":
        return break_glass_approval(**common)
    raise legacy.StateError(
        f"unsupported coordinated claim approval level {level or '<missing>'}"
    )
''',
)

replace_once(
    v2,
    '''        approved: bool = False,
        approval_source: str = "coordinated claim intent",
''',
    '''        approved: bool = False,
        break_glass: bool = False,
        approval_source: str = "coordinated claim intent",
''',
)

replace_once(
    v2,
    '''                    and task.policy == "review-before-effect"
                    and approved
                ):
                    approval = explicit_operator_approval(
                        source=approval_source,
                        approved=True,
                        reviewer=worker_id,
                        reference=run_id,
                        task_id=task.id,
                        scope="repository_mutation",
                    )
                    require_approval(
                        "repository_mutation",
                        approval,
                        expected_reference=run_id,
                        task_id=task.id,
                    )
''',
    '''                    and task.policy == "review-before-effect"
                    and (approved or break_glass)
                ):
                    approval, _approval_decision = _coordinated_claim_approval(
                        task,
                        worker_id=worker_id,
                        run_id=run_id,
                        approved=approved,
                        break_glass=break_glass,
                        source=approval_source,
                    )
''',
)

replace_once(
    v2,
    '''        if approval_evidence is None:
            approval = explicit_operator_approval(
                source=approval_source,
                approved=approved,
                reviewer=worker_id,
                reference=run_id,
                task_id=selected.id,
                scope="repository_mutation",
            )
            require_approval(
                "repository_mutation",
                approval,
                expected_reference=run_id,
                task_id=selected.id,
            )
            approval_evidence = approval.as_dict()
''',
    '''        if approval_evidence is None:
            approval, _approval_decision = _coordinated_claim_approval(
                selected,
                worker_id=worker_id,
                run_id=run_id,
                approved=approved,
                break_glass=break_glass,
                source=approval_source,
            )
            approval_evidence = approval.as_dict()
''',
)

replace_once(
    v2,
    '''        approval_data = intent["operator_approval"]
        approval = explicit_operator_approval(
            source=approval_data.get("source", "coordinated claim intent"),
            approved=approval_data.get("approved") is True,
            reviewer=approval_data.get("reviewer"),
            reference=approval_data.get("reference"),
            task_id=approval_data.get("task_id"),
            scope=approval_data.get("scope"),
            note=approval_data.get("note"),
        )
        approval_decision = require_approval(
            "repository_mutation",
            approval,
            expected_reference=intent["run_id"],
            task_id=task.id,
        )
''',
    '''        approval_data = intent["operator_approval"]
        approval = _coordinated_approval_from_dict(approval_data)
        approval_action_class = _coordinated_task_action_class(task)
        approval_decision = require_approval(
            approval_action_class,
            approval,
            expected_reference=intent["run_id"],
            task_id=task.id,
        )
''',
)

replace_once(
    cli,
    '''    claim_intent.add_argument("--approve", action="store_true")
    claim_intent.add_argument("--approval-source", default="cli claim-intent --approve")
''',
    '''    claim_approval = claim_intent.add_mutually_exclusive_group()
    claim_approval.add_argument("--approve", action="store_true")
    claim_approval.add_argument("--break-glass", action="store_true")
    claim_intent.add_argument(
        "--approval-source",
        default="cli claim-intent explicit approval",
    )
''',
)

replace_once(
    cli,
    '''                approved=args.approve,
                approval_source=args.approval_source,
''',
    '''                approved=args.approve,
                break_glass=args.break_glass,
                approval_source=args.approval_source,
''',
)

insert_before(
    tests,
    "\ndef coordinated_lease_database(\n",
    r'''


def declare_runtime_mutation(root: Path, task_id: str) -> None:
    task_path = root / f"registry/tasks/{task_id}.json"
    task = json.loads(task_path.read_text())
    task["execution"]["approval"] = {
        "action_class": "runtime_mutation",
        "required_level": "break_glass",
        "note": "test-only live runtime effect",
    }
    task_path.write_text(json.dumps(task))
''',
)

insert_before(
    tests,
    "\ndef test_coordinated_claim_handoff_does_not_require_absent_lease(\n",
    r'''


def test_coordinated_runtime_claim_rejects_operator_approval(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    with pytest.raises(StateError, match="approval required for runtime_mutation"):
        dispatcher.claim_intent(
            "operator",
            ("repository",),
            task_id=task_id,
            approved=True,
            approval_source="test operator approval",
        )

    assert store.list_runs() == []


def test_coordinated_runtime_claim_binds_break_glass_through_commit(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        break_glass=True,
        approval_source="test explicit break glass",
    )["intent"]

    approval = intent["operator_approval"]
    assert approval["level"] == "break_glass"
    assert approval["scope"] == ["runtime_mutation"]
    assert approval["reference"] == intent["run_id"]
    assert approval["task_id"] == task_id

    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    claimed = dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    decision = claimed["envelope"]["operator_approval"]
    assert decision["action_class"] == "runtime_mutation"
    assert decision["required_level"] == "break_glass"
    assert decision["allowed"] is True
    assert decision["evidence"]["level"] == "break_glass"
    assert registry.tasks[task_id].state == "ready"
    assert store.run(intent["run_id"])["state"] == "assigned"


def test_coordinated_runtime_claim_rejects_rehashed_approval_downgrade(
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
    intent["operator_approval"]["level"] = "operator"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="approval required for runtime_mutation"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    assert store.list_runs() == []
''',
)

replace_once(
    tests,
    '''    assert intent_args.command == "claim-intent"
    assert intent_args.approve is True
''',
    '''    assert intent_args.command == "claim-intent"
    assert intent_args.approve is True
    assert intent_args.break_glass is False

    break_glass_args = bureau_cli.parser().parse_args(
        [
            "claim-intent",
            "--worker",
            "operator",
            "--task-id",
            "TASK-1",
            "--capability",
            "repository",
            "--break-glass",
        ]
    )
    assert break_glass_args.approve is False
    assert break_glass_args.break_glass is True

    with pytest.raises(SystemExit):
        bureau_cli.parser().parse_args(
            [
                "claim-intent",
                "--worker",
                "operator",
                "--approve",
                "--break-glass",
            ]
        )
''',
)

print("Applied PR #1322 coordinated approval enforcement patch.")
