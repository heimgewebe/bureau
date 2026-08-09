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
    """def _lifecycle_diagnostics_from_overlays(
    operational_registry: Registry,
    source_registry: Registry,
    overlays: dict[str, str],
) -> list[dict[str, Any]]:""",
    """def _lifecycle_diagnostics_from_overlays(
    operational_registry: Registry,
    source_registry: Registry,
    overlays: dict[str, str],
    store: StateStore,
) -> list[dict[str, Any]]:""",
    "lifecycle diagnostics signature",
)

source = replace_once(
    source,
    """        recommendation = _lifecycle_recommendation(initiative.state, states)
        source = source_registry.initiatives[initiative.id]""",
    """        recommendation = _lifecycle_recommendation(initiative.state, states)
        completion_verification: dict[str, dict[str, Any]] = {}
        verification_required_task_ids: list[str] = []
        if recommendation == "completion-ready":
            for task_id, state in states.items():
                if state != "verified":
                    continue
                try:
                    completion_verification[task_id] = verification_stamp(
                        operational_registry, store, task_id
                    )
                except legacy.StateError:
                    verification_required_task_ids.append(task_id)
            if verification_required_task_ids:
                recommendation = "verification-required"
        source = source_registry.initiatives[initiative.id]""",
    "completion evidence gate",
)

source = replace_once(
    source,
    """                "registry_state": source.state,
                "recommended_state": recommendation,
                "task_states": states,
                "consistent": initiative.state == recommendation,""",
    """                "registry_state": source.state,
                "recommended_state": recommendation,
                "task_states": states,
                "completion_verification": completion_verification,
                "verification_required_task_ids": verification_required_task_ids,
                "consistent": initiative.state == recommendation,""",
    "completion evidence diagnostics",
)

source = replace_once(
    source,
    """    return _lifecycle_diagnostics_from_overlays(
        operational_registry, registry, overlays
    )""",
    """    return _lifecycle_diagnostics_from_overlays(
        operational_registry, registry, overlays, store
    )""",
    "lifecycle diagnostics caller",
)

source = replace_once(
    source,
    """        child_states = {
            child_task_id: overlays.get(
                child_task_id, registry.tasks[child_task_id].state
            )
            for child_task_id in parent_child.child_task_ids
        }
        candidates.append(""",
    """        child_states = {
            child_task_id: overlays.get(
                child_task_id, registry.tasks[child_task_id].state
            )
            for child_task_id in parent_child.child_task_ids
        }
        child_verification: dict[str, dict[str, Any]] = {}
        try:
            for child_task_id, state in child_states.items():
                if state == "verified":
                    child_verification[child_task_id] = verification_stamp(
                        registry, store, child_task_id
                    )
        except legacy.StateError:
            continue
        candidates.append(""",
    "child verification gate",
)

source = replace_once(
    source,
    """                "dependency_states": dependency_states,
                "dependency_verification": dependency_verification,
                "child_task_states": child_states,
                "gate": "schema-and-evidence-deterministic-structural-gates",""",
    """                "dependency_states": dependency_states,
                "dependency_verification": dependency_verification,
                "child_task_states": child_states,
                "child_task_verification": child_verification,
                "gate": "schema-and-evidence-deterministic-structural-gates",""",
    "child verification candidate evidence",
)

source = replace_once(
    source,
    """    diagnostics = _lifecycle_diagnostics_from_overlays(
        operational_registry, registry, projected_overlays
    )""",
    """    diagnostics = _lifecycle_diagnostics_from_overlays(
        operational_registry, registry, projected_overlays, store
    )""",
    "reconcile lifecycle diagnostics caller",
)

source = replace_once(
    source,
    """                "initiative_id": item["initiative_id"],
                "from_state": item["declared_state"],
                "to_state": item["recommended_state"],
                "task_states": item["task_states"],
            }""",
    """                "initiative_id": item["initiative_id"],
                "from_state": item["declared_state"],
                "to_state": item["recommended_state"],
                "task_states": item["task_states"],
                "completion_verification": item["completion_verification"],
            }""",
    "initiative candidate evidence",
)

source = replace_once(
    source,
    """        fresh_projected_overlays = dict(fresh_overlays)
        for candidate in fresh_task_candidates:
            fresh_projected_overlays[candidate["task_id"]] = candidate["to_state"]

        for candidate in initiative_candidates:""",
    """        fresh_projected_overlays = dict(fresh_overlays)
        for candidate in fresh_task_candidates:
            fresh_projected_overlays[candidate["task_id"]] = candidate["to_state"]
        fresh_diagnostics = {
            item["initiative_id"]: item
            for item in _lifecycle_diagnostics_from_overlays(
                operational_registry, registry, fresh_projected_overlays, store
            )
        }

        for candidate in initiative_candidates:""",
    "fresh lifecycle evidence diagnostics",
)

source = replace_once(
    source,
    """            fresh_task_states = {
                task.id: fresh_projected_overlays.get(task.id, task.state)
                for task in operational_registry.tasks.values()
                if task.initiative == initiative_id
            }
            if (
                fresh_task_states != candidate["task_states"]
                or _lifecycle_recommendation(current_state, fresh_task_states)
                != candidate["to_state"]
            ):
                raise legacy.StateError(
                    f"initiative {initiative_id} lifecycle inputs changed during reconcile"
                )""",
    """            fresh_diagnostic = fresh_diagnostics[initiative_id]
            if (
                fresh_diagnostic["task_states"] != candidate["task_states"]
                or fresh_diagnostic["recommended_state"] != candidate["to_state"]
                or fresh_diagnostic["completion_verification"]
                != candidate["completion_verification"]
            ):
                raise legacy.StateError(
                    f"initiative {initiative_id} lifecycle inputs changed during reconcile"
                )""",
    "fresh initiative lifecycle evidence validation",
)

source = replace_once(
    source,
    """        raw["state"] = "completed"
        raw["commitment"] = "completed"
        metadata = raw.setdefault("metadata", {})
        lifecycle = metadata.setdefault("lifecycle", {})
        lifecycle["completed_at"] = legacy.utc_now()""",
    """        was_completed = raw.get("state") == "completed"
        raw["state"] = "completed"
        raw["commitment"] = "completed"
        metadata = raw.setdefault("metadata", {})
        lifecycle = metadata.setdefault("lifecycle", {})
        completed_at = lifecycle.get("completed_at")
        if (
            not was_completed
            or not isinstance(completed_at, str)
            or not completed_at.strip()
        ):
            lifecycle["completed_at"] = legacy.utc_now()""",
    "completion timestamp preservation",
)

source_path.write_text(source, encoding="utf-8")


tests_path = Path("tests/test_v2.py")
tests = tests_path.read_text(encoding="utf-8")

tests = replace_once(
    tests,
    """    child_metadata = dict(child_spec.get("metadata", {}))
    child_metadata["parent_task"] = parent_id
    child_spec["metadata"] = child_metadata
    store.put_task_spec(
        child_spec,
        idempotency_key="lifecycle-child-verified",
        expected_revision=child["revision"],
        source="test",
    )""",
    """    child_metadata = dict(child_spec.get("metadata", {}))
    child_metadata["parent_task"] = parent_id
    child_spec["metadata"] = child_metadata
    child_metadata["verification"] = {
        "task_sha256": task_revision_sha256(child_spec),
        "plan_sha256": plan_sha256(registry, child_spec["initiative"]),
    }
    child_spec["metadata"] = child_metadata
    store.put_task_spec(
        child_spec,
        idempotency_key="lifecycle-child-verified",
        expected_revision=child["revision"],
        source="test",
    )""",
    "bind existing parent-child positive test evidence",
)

tests_to_insert = r'''
def test_lifecycle_reconcile_rejects_unbound_verified_child(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    parent_id, child_id = sorted(registry.tasks)

    parent = store.task_spec(parent_id)
    assert parent is not None
    parent_spec = dict(parent["spec"])
    parent_spec["state"] = "planned"
    store.put_task_spec(
        parent_spec,
        idempotency_key="lifecycle-unbound-child-parent-planned",
        expected_revision=parent["revision"],
        source="test",
    )

    child = store.task_spec(child_id)
    assert child is not None
    child_spec = dict(child["spec"])
    child_spec["state"] = "verified"
    child_metadata = dict(child_spec.get("metadata", {}))
    child_metadata["parent_task"] = parent_id
    child_spec["metadata"] = child_metadata
    store.put_task_spec(
        child_spec,
        idempotency_key="lifecycle-unbound-child-verified",
        expected_revision=child["revision"],
        source="test",
    )

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["task_candidate_count"] == 0


def test_lifecycle_completion_requires_verified_task_evidence(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    current = store.task_spec(task_id)
    assert current is not None
    spec = dict(current["spec"])
    spec["state"] = "verified"
    store.put_task_spec(
        spec,
        idempotency_key="lifecycle-unbound-verified-for-completion",
        expected_revision=current["revision"],
        source="test",
    )
    initiative_path = root / "registry/initiatives/main.json"
    initiative_before = initiative_path.read_bytes()

    diagnostic = lifecycle_diagnostics(registry, store)[0]
    assert diagnostic["recommended_state"] == "verification-required"
    assert diagnostic["verification_required_task_ids"] == [task_id]
    assert diagnostic["completion_verification"] == {}

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["candidate_count"] == 0
    assert "verification-required" in preview["excluded_recommendations"]
    assert close_ready_initiatives(registry, store) == []
    assert initiative_path.read_bytes() == initiative_before


def test_close_ready_preserves_completed_at_when_state_store_retry_needed(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    preliminary = Registry.load(root)
    task = json.loads(task_path.read_text())
    task["state"] = "verified"
    task["metadata"] = {
        "verification": {
            "task_sha256": task_revision_sha256(task),
            "plan_sha256": plan_sha256(preliminary, task["initiative"]),
        }
    }
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))

    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    completed_at = "2026-08-09T00:00:00Z"
    initiative["state"] = "completed"
    initiative["commitment"] = "completed"
    metadata = initiative.setdefault("metadata", {})
    lifecycle = metadata.setdefault("lifecycle", {})
    lifecycle["completed_at"] = completed_at
    initiative_path.write_text(json.dumps(initiative))

    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.set_initiative_state(initiative["id"], "completion-ready")

    changed = close_ready_initiatives(registry, store)
    assert changed == [
        {"initiative_id": initiative["id"], "path": str(initiative_path)}
    ]
    persisted = json.loads(initiative_path.read_text())
    assert persisted["metadata"]["lifecycle"]["completed_at"] == completed_at
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            (initiative["id"],),
        ).fetchone()
    assert row is not None
    assert row["state"] == "completed"
'''.lstrip("\n")

marker = "def test_lifecycle_reconcile_projects_completion_ready_without_completing("
tests = replace_once(
    tests,
    marker,
    tests_to_insert + "\n\n" + marker,
    "review regression insertion",
)

tests_path.write_text(tests, encoding="utf-8")
