from pathlib import Path

SOURCE = Path("src/bureau/v2.py")
TESTS = Path("tests/test_v2.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def replace_count(text: str, old: str, new: str, count: int, label: str) -> str:
    observed = text.count(old)
    if observed != count:
        raise SystemExit(f"{label}: expected {count} matches, found {observed}")
    return text.replace(old, new)


source = SOURCE.read_text(encoding="utf-8")
source = replace_once(
    source,
    '''        *,
        projection_resource: str | None = None,
    ) -> list[str]:
        result: list[str] = []
        initiative = self.registry.initiatives[task.initiative]
''',
    '''        *,
        projection_resource: str | None = None,
        initiative_registry: Registry | None = None,
    ) -> list[str]:
        result: list[str] = []
        initiative_source = initiative_registry or self.registry
        initiative = initiative_source.initiatives[task.initiative]
''',
    "reasons initiative override",
)

shared_claim_state = '''            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection) + open_pr_reservations
            overlays = self.store.overlays(connection, self.registry)
            rejected: list[dict[str, Any]] = []
'''
shared_claim_state_new = '''            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection) + open_pr_reservations
            overlays = self.store.overlays(connection, self.registry)
            fresh_source_registry = Registry.load(self.source_registry.root)
            fresh_initiative_registry, _ = _state_store_initiative_registry(
                fresh_source_registry, self.store, connection=connection
            )
            rejected: list[dict[str, Any]] = []
'''
source = replace_count(
    source,
    shared_claim_state,
    shared_claim_state_new,
    2,
    "claim-intent and claim-next initiative refresh",
)

source = replace_once(
    source,
    '''                reasons = self.reasons(task, capabilities_set, runs, reservations, overlays)
''',
    '''                reasons = self.reasons(
                    task,
                    capabilities_set,
                    runs,
                    reservations,
                    overlays,
                    initiative_registry=fresh_initiative_registry,
                )
''',
    "claim-intent reasons refresh",
)
source = replace_once(
    source,
    '''                reasons = self.reasons(task, worker_capabilities, runs, reservations, overlays)
''',
    '''                reasons = self.reasons(
                    task,
                    worker_capabilities,
                    runs,
                    reservations,
                    overlays,
                    initiative_registry=fresh_initiative_registry,
                )
''',
    "claim-next reasons refresh",
)

source = replace_once(
    source,
    '''            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection) + fresh_open_pr_reservations
            overlays = self.store.overlays(connection, self.registry)
            attempt = (
''',
    '''            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection) + fresh_open_pr_reservations
            overlays = self.store.overlays(connection, self.registry)
            fresh_source_registry = Registry.load(self.source_registry.root)
            fresh_initiative_registry, _ = _state_store_initiative_registry(
                fresh_source_registry, self.store, connection=connection
            )
            attempt = (
''',
    "claim-commit initiative refresh",
)
source = replace_once(
    source,
    '''            reasons = self.reasons(task, set(intent["capabilities"]), runs, reservations, overlays)
''',
    '''            reasons = self.reasons(
                task,
                set(intent["capabilities"]),
                runs,
                reservations,
                overlays,
                initiative_registry=fresh_initiative_registry,
            )
''',
    "claim-commit reasons refresh",
)
SOURCE.write_text(source, encoding="utf-8")


tests = TESTS.read_text(encoding="utf-8")
regressions = r'''


def test_claim_next_rechecks_initiative_state_inside_transaction(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    store.set_initiative_state("BUR-TEST-001", "completed")

    with pytest.raises(NoEligibleTask, match="initiative state is completed"):
        dispatcher.claim_next(
            "stale-initiative-worker",
            ("repository",),
            reconcile_first=False,
        )

    assert store.list_runs() == []


def test_commit_claim_intent_rechecks_initiative_state_inside_transaction(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )

    issued = dispatcher.claim_intent(
        "stale-initiative-operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="initiative-state-race-regression",
        idempotency_key="initiative-state-race-regression",
    )
    store.set_initiative_state("BUR-TEST-001", "completed")

    with pytest.raises(StateError, match="initiative state is completed"):
        dispatcher.commit_claim_intent(issued["intent"], None)

    assert store.list_runs() == []
'''.rstrip() + "\n"
if "def test_claim_next_rechecks_initiative_state_inside_transaction(" in tests:
    raise SystemExit("claim initiative regression tests already exist")
TESTS.write_text(tests.rstrip() + regressions, encoding="utf-8")
