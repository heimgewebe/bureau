from __future__ import annotations

import json
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


# 1) Reserve and authenticate the no-run closeout idempotency namespace.
task_specs_path = Path("src/bureau/task_specs.py")
task_specs = task_specs_path.read_text(encoding="utf-8")
task_specs = replace_once(
    task_specs,
    'TASK_SPEC_PROJECTION_SCHEMA_VERSION = 1\n',
    'TASK_SPEC_PROJECTION_SCHEMA_VERSION = 1\n'
    'RUNTIME_REFRESH_NO_RUN_CLOSEOUT_IDEMPOTENCY_PREFIX = "runtime-refresh-no-run-closeout:"\n',
    "task-spec closeout prefix constant",
)

get_receipt = r'''

def get_mutation_receipt(
    connection: sqlite3.Connection, idempotency_key: str
) -> dict[str, Any] | None:
    validate_schema(connection)
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise TaskSpecError("TaskSpec idempotency key must be non-empty")
    row = connection.execute(
        "SELECT idempotency_key,task_id,expected_revision,requested_sha256,"
        "resulting_revision,created_at FROM task_spec_mutations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    task_id = row["task_id"]
    requested_sha256 = row["requested_sha256"]
    resulting_revision = row["resulting_revision"]
    expected_revision = row["expected_revision"]
    created_at = row["created_at"]
    if not isinstance(task_id, str) or not task_id:
        raise TaskSpecError("TaskSpec mutation receipt task_id is invalid")
    if (
        expected_revision is not None
        and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        )
    ):
        raise TaskSpecError("TaskSpec mutation receipt expected revision is invalid")
    if (
        not isinstance(resulting_revision, int)
        or isinstance(resulting_revision, bool)
        or resulting_revision < 1
    ):
        raise TaskSpecError("TaskSpec mutation receipt resulting revision is invalid")
    if not isinstance(requested_sha256, str) or not requested_sha256:
        raise TaskSpecError("TaskSpec mutation receipt digest is invalid")
    if not isinstance(created_at, str) or not created_at:
        raise TaskSpecError("TaskSpec mutation receipt timestamp is invalid")
    resulting_task_spec = _validated_row(
        _revision_row(connection, task_id, resulting_revision)
    )
    if resulting_task_spec["spec_sha256"] != requested_sha256:
        raise TaskSpecError("TaskSpec mutation receipt digest mismatch")
    return {
        "idempotency_key": idempotency_key,
        "task_id": task_id,
        "expected_revision": expected_revision,
        "requested_sha256": requested_sha256,
        "resulting_revision": resulting_revision,
        "created_at": created_at,
        "resulting_task_spec": resulting_task_spec,
    }


def _validate_runtime_refresh_no_run_closeout_mutation(
    spec: Mapping[str, Any], idempotency_key: str
) -> dict[str, Any]:
    canonical = _canonical_spec(spec)
    metadata = canonical.get("metadata")
    closeout = metadata.get("runtime_closeout") if isinstance(metadata, Mapping) else None
    result_sha256 = closeout.get("runtime_result_sha256") if isinstance(closeout, Mapping) else None
    if (
        not isinstance(closeout, Mapping)
        or closeout.get("kind") != "bureau_runtime_refresh_no_run_closeout"
        or closeout.get("status") != "verified"
        or closeout.get("task_id") != canonical["id"]
        or not isinstance(result_sha256, str)
        or len(result_sha256) != 64
        or any(character not in "0123456789abcdef" for character in result_sha256)
    ):
        raise TaskSpecError("runtime-refresh no-run closeout mutation contract is invalid")
    expected_key = (
        f"{RUNTIME_REFRESH_NO_RUN_CLOSEOUT_IDEMPOTENCY_PREFIX}"
        f"{canonical['id']}:{result_sha256}"
    )
    if idempotency_key != expected_key:
        raise TaskSpecError("runtime-refresh no-run closeout idempotency binding is invalid")
    return canonical


def put_runtime_refresh_no_run_closeout(
    connection: sqlite3.Connection,
    spec: Mapping[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int | None,
) -> dict[str, Any]:
    canonical = _validate_runtime_refresh_no_run_closeout_mutation(spec, idempotency_key)
    try:
        validate_task_write(canonical, f"TaskSpec:{canonical['id']}")
    except (DocumentSchemaError, AcceptanceContractError) as exc:
        raise TaskSpecError(str(exc)) from exc
    return _put_validated_material(
        connection,
        canonical,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        source="runtime-refresh-no-run-closeout",
    )
'''
task_specs = replace_once(
    task_specs,
    '\ndef current_projection(connection: sqlite3.Connection) -> dict[str, Any]:\n',
    get_receipt + '\n\ndef current_projection(connection: sqlite3.Connection) -> dict[str, Any]:\n',
    "task-spec receipt accessor insertion",
)

generic_put_anchor = r'''    canonical = _canonical_spec(spec)
    try:
        validate_task_write(canonical, f"TaskSpec:{canonical['id']}")
'''
generic_put_replacement = r'''    canonical = _canonical_spec(spec)
    if idempotency_key.startswith(RUNTIME_REFRESH_NO_RUN_CLOSEOUT_IDEMPOTENCY_PREFIX):
        raise TaskSpecError(
            "runtime-refresh no-run closeout idempotency namespace is reserved"
        )
    try:
        validate_task_write(canonical, f"TaskSpec:{canonical['id']}")
'''
task_specs = replace_once(
    task_specs,
    generic_put_anchor,
    generic_put_replacement,
    "generic reserved namespace guard",
)
task_specs_path.write_text(task_specs, encoding="utf-8")


# 2) Expose the typed receipt/read and specialized closeout write through StateStore.
v2_path = Path("src/bureau/v2.py")
v2 = v2_path.read_text(encoding="utf-8")
state_store_methods = r'''    def task_spec_mutation_receipt(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            try:
                return task_specs.get_mutation_receipt(connection, idempotency_key)
            except task_specs.TaskSpecError as exc:
                raise legacy.StateError(str(exc)) from exc

    def put_runtime_refresh_no_run_closeout_task_spec(
        self,
        spec: dict[str, Any],
        *,
        idempotency_key: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        with self.immediate() as connection:
            try:
                return task_specs.put_runtime_refresh_no_run_closeout(
                    connection,
                    spec,
                    idempotency_key=idempotency_key,
                    expected_revision=expected_revision,
                )
            except task_specs.TaskSpecError as exc:
                raise legacy.StateError(str(exc)) from exc

'''
v2 = replace_once(
    v2,
    '    def task_spec_by_digest(\n',
    state_store_methods + '    def task_spec_by_digest(\n',
    "StateStore typed closeout methods",
)
v2_path.write_text(v2, encoding="utf-8")


# 3) Make runtime closeout use the reserved writer and anchor replay to its receipt.
runtime_path = Path("src/bureau/runtime_refresh.py")
runtime = runtime_path.read_text(encoding="utf-8")
put_anchor = r'''    try:
        return store.put_task_spec(
            spec,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            source=source,
        )
'''
put_replacement = r'''    try:
        if source == "runtime-refresh-no-run-closeout":
            return store.put_runtime_refresh_no_run_closeout_task_spec(
                spec,
                idempotency_key=idempotency_key,
                expected_revision=expected_revision,
            )
        return store.put_task_spec(
            spec,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            source=source,
        )
'''
runtime = replace_once(
    runtime,
    put_anchor,
    put_replacement,
    "runtime specialized closeout writer",
)

start = runtime.index("def _historical_no_run_closeout_acceptance_evidence(\n")
end = runtime.index("def _validate_no_run_acceptance_replay_binding(\n", start)
new_history_helper = r'''def _historical_no_run_closeout_acceptance_evidence(
    *,
    store: Any,
    closeout: dict[str, Any],
) -> dict[str, Any]:
    idempotency_key = (
        f"runtime-refresh-no-run-closeout:{closeout['task_id']}:"
        f"{closeout['runtime_result_sha256']}"
    )
    try:
        receipt = store.task_spec_mutation_receipt(idempotency_key)
    except (legacy.StateError, OSError, sqlite3.Error) as exc:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "cannot authenticate the immutable no-run closeout mutation receipt",
            details={"error": str(exc)},
        ) from exc
    if receipt is None or receipt.get("task_id") != closeout["task_id"]:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "no-run closeout has no matching immutable mutation receipt",
            details={"idempotency_key": idempotency_key},
        )
    historical_task_spec = receipt.get("resulting_task_spec")
    if (
        not isinstance(historical_task_spec, dict)
        or historical_task_spec.get("revision") != receipt.get("resulting_revision")
        or historical_task_spec.get("spec_sha256") != receipt.get("requested_sha256")
        or historical_task_spec.get("source") != "runtime-refresh-no-run-closeout"
    ):
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "no-run closeout mutation receipt does not authenticate its TaskSpec revision",
            details={"idempotency_key": idempotency_key},
        )
    historical_spec = historical_task_spec.get("spec")
    historical_metadata = (
        historical_spec.get("metadata") if isinstance(historical_spec, dict) else None
    )
    historical_closeout_value = (
        historical_metadata.get("runtime_closeout")
        if isinstance(historical_metadata, dict)
        else None
    )
    try:
        historical_closeout = _validated_runtime_closeout(historical_closeout_value)
    except RuntimeRefreshError as exc:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "receipt-bound no-run closeout revision contains invalid closeout evidence",
            details={
                "revision": historical_task_spec.get("revision"),
                "error": str(exc),
            },
        ) from exc
    stable_closeout = {
        key: value for key, value in closeout.items() if key != "acceptance_evidence"
    }
    historical_stable = {
        key: value
        for key, value in historical_closeout.items()
        if key != "acceptance_evidence"
    }
    if historical_stable != stable_closeout:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "no-run closeout no longer matches its receipt-bound immutable revision",
            details={"revision": historical_task_spec["revision"]},
        )
    historical_acceptance_evidence = historical_closeout.get("acceptance_evidence")
    if historical_acceptance_evidence is None:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "typed no-run acceptance evidence has no receipt-bound historical origin",
            details={"revision": historical_task_spec["revision"]},
        )
    return historical_acceptance_evidence


'''
runtime = runtime[:start] + new_history_helper + runtime[end:]
runtime_path.write_text(runtime, encoding="utf-8")


# 4) Migrate all current Registry source-precondition bootstrap authorities with
# semantically scoped criterion -> evidence mappings.
def evidence_for(criterion_id: str) -> list[str]:
    if criterion_id.startswith("fresh-current-main"):
        return ["approval-intent", "runtime-result"]
    if criterion_id in {
        "source-ancestry-precondition",
        "source-precondition-before-prepare-and-apply",
    }:
        return ["approval-intent", "runtime-result", "source-precondition"]
    if criterion_id == "protected-publication-and-missing-only-adoption":
        return ["approval-intent", "state-store-integrity"]
    if criterion_id == "single-runtime-effect":
        return ["runtime-result", "single-use-history", "run-lifecycle"]
    if criterion_id == "immutable-runtime-readback":
        return ["runtime-result", "immutable-readback", "state-store-integrity"]
    if criterion_id == "hall-of-memory-resource-visible":
        return ["immutable-readback", "state-store-integrity"]
    if criterion_id in {
        "runtime-only-scope",
        "single-use-consumption-and-runtime-only-scope",
    }:
        return ["runtime-result", "single-use-history", "lease-release", "run-lifecycle"]
    raise SystemExit(f"unmapped runtime authority acceptance criterion: {criterion_id}")

migrated: list[str] = []
for path in sorted(Path("registry/tasks").glob("*.json")):
    spec = json.loads(path.read_text(encoding="utf-8"))
    metadata = spec.get("metadata")
    authority = metadata.get("runtime_refresh_authority") if isinstance(metadata, dict) else None
    if not isinstance(authority, dict) or "source_precondition" not in authority:
        continue
    if authority.get("mode") != "single-use-target-bound-source-precondition-v1":
        raise SystemExit(f"source-precondition authority on unsupported mode: {path}")
    if "no_run_closeout_acceptance" in authority:
        raise SystemExit(f"unexpected pre-existing no-run mapping: {path}")
    acceptance = spec.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise SystemExit(f"runtime authority has no acceptance: {path}")
    criteria: dict[str, dict[str, object]] = {}
    for criterion in acceptance:
        criterion_id = criterion.get("id") if isinstance(criterion, dict) else None
        if not isinstance(criterion_id, str) or not criterion_id:
            raise SystemExit(f"runtime authority criterion has no id: {path}")
        criteria[criterion_id] = {
            "verifier": "runtime-refresh-no-run-evidence-v1",
            "required_evidence": evidence_for(criterion_id),
        }
    if not any(
        "source-precondition" in item["required_evidence"] for item in criteria.values()
    ):
        raise SystemExit(f"runtime authority lacks source-precondition evidence mapping: {path}")
    authority["no_run_closeout_acceptance"] = {
        "schema_version": 1,
        "kind": "bureau_runtime_refresh_no_run_acceptance_contract",
        "criteria": criteria,
    }
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    migrated.append(path.as_posix())

if len(migrated) != 6:
    raise SystemExit(f"expected six source-precondition Registry authorities, got {migrated}")


# 5) Regression coverage: reserved namespace/receipt, source-label impersonation,
# and repository-wide closeout-readiness of source-precondition authorities.
task_tests_path = Path("tests/test_task_specs.py")
task_tests = task_tests_path.read_text(encoding="utf-8")
receipt_test_anchor = r'''def test_reverting_to_prior_content_creates_a_new_revision(tmp_path: Path) -> None:
'''
receipt_test = r'''def test_reserved_runtime_closeout_namespace_and_receipt_are_typed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.put_task_spec(
        _spec(),
        idempotency_key="seed-closeout",
        expected_revision=None,
        source="test",
    )
    closeout_spec = _spec(title="verified")
    closeout_spec["metadata"]["runtime_closeout"] = {
        "kind": "bureau_runtime_refresh_no_run_closeout",
        "status": "verified",
        "task_id": "TEST-T001",
        "runtime_result_sha256": "a" * 64,
    }
    key = "runtime-refresh-no-run-closeout:TEST-T001:" + "a" * 64

    with pytest.raises(StateError, match="namespace is reserved"):
        store.put_task_spec(
            closeout_spec,
            idempotency_key=key,
            expected_revision=1,
            source="runtime-refresh-no-run-closeout",
        )

    written = store.put_runtime_refresh_no_run_closeout_task_spec(
        closeout_spec,
        idempotency_key=key,
        expected_revision=1,
    )
    receipt = store.task_spec_mutation_receipt(key)
    assert receipt is not None
    assert receipt["task_id"] == "TEST-T001"
    assert receipt["requested_sha256"] == written["spec_sha256"]
    assert receipt["resulting_revision"] == written["revision"]
    assert receipt["resulting_task_spec"]["spec"] == closeout_spec
    assert receipt["resulting_task_spec"]["source"] == "runtime-refresh-no-run-closeout"
    assert store.task_spec_mutation_receipt("missing") is None


'''
task_tests = replace_once(
    task_tests,
    receipt_test_anchor,
    receipt_test + receipt_test_anchor,
    "task-spec reserved receipt regression",
)
task_tests_path.write_text(task_tests, encoding="utf-8")

runtime_tests_path = Path("tests/test_runtime_refresh.py")
runtime_tests = runtime_tests_path.read_text(encoding="utf-8")
revise_old = r'''def revise_authority(
    store: StateStore,
    task_id: str,
    mutate: Any,
    *,
    key: str,
) -> dict[str, Any]:
    current = store.task_spec(task_id)
    assert current is not None
    spec = json.loads(json.dumps(current["spec"]))
    mutate(spec)
    return store.put_task_spec(
        spec,
        idempotency_key=key,
        expected_revision=current["revision"],
        source="test",
    )
'''
revise_new = r'''def revise_authority(
    store: StateStore,
    task_id: str,
    mutate: Any,
    *,
    key: str,
    source: str = "test",
) -> dict[str, Any]:
    current = store.task_spec(task_id)
    assert current is not None
    spec = json.loads(json.dumps(current["spec"]))
    mutate(spec)
    return store.put_task_spec(
        spec,
        idempotency_key=key,
        expected_revision=current["revision"],
        source=source,
    )
'''
runtime_tests = replace_once(
    runtime_tests,
    revise_old,
    revise_new,
    "revise_authority source override",
)
runtime_tests = replace_once(
    runtime_tests,
    '        key=f"tamper:acceptance-history:{field}",\n    )\n',
    '        key=f"tamper:acceptance-history:{field}",\n'
    '        source="runtime-refresh-no-run-closeout",\n'
    '    )\n',
    "source-label impersonation regression",
)

registry_regression = r'''

def test_registry_source_precondition_authorities_have_complete_no_run_acceptance_contracts(
) -> None:
    paths: list[Path] = []
    for path in sorted((Path(__file__).parents[1] / "registry/tasks").glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        metadata = spec.get("metadata")
        authority = (
            metadata.get("runtime_refresh_authority") if isinstance(metadata, dict) else None
        )
        if not isinstance(authority, dict) or "source_precondition" not in authority:
            continue
        paths.append(path)
        assert authority["mode"] == refresh.RUNTIME_AUTHORITY_MODE_SOURCE_PRECONDITION
        contract = refresh._validated_no_run_acceptance_contract(
            spec=spec, authority=authority
        )
        assert set(contract["criteria"]) == {
            criterion["id"] for criterion in spec["acceptance"]
        }
        assert any(
            "source-precondition" in item["required_evidence"]
            for item in contract["criteria"].values()
        )
    assert len(paths) == 6
'''
runtime_tests = runtime_tests.rstrip() + registry_regression + "\n"
runtime_tests_path.write_text(runtime_tests, encoding="utf-8")


# 6) Document the immutable receipt boundary.
docs_path = Path("docs/bureau-runtime-refresh-v1.md")
docs = docs_path.read_text(encoding="utf-8")
doc_anchor = (
    "Bei einer Authority mit `source_precondition` muss mindestens ein Acceptance-Kriterium\n"
    "explizit die Evidenzklasse `source-precondition` verlangen. Außerdem müssen historischer\n"
)
doc_replacement = (
    "Bei einer Authority mit `source_precondition` muss mindestens ein Acceptance-Kriterium\n"
    "explizit die Evidenzklasse `source-precondition` verlangen. Die geschützten Registry-\n"
    "Bootstrap-Authorities führen deshalb für jedes eingefrorene Kriterium ein explizites\n"
    "`no_run_closeout_acceptance`-Mapping. Außerdem müssen historischer\n"
)
docs = replace_once(docs, doc_anchor, doc_replacement, "acceptance migration docs")
receipt_doc_anchor = (
    "Ein Runtime-Refresh kann als Bootstrap stattfinden, bevor die neue Runtime einen normalen\n"
)
receipt_doc_replacement = (
    "Der erfolgreiche No-Run-Closeout wird über den reservierten TaskSpec-Idempotency-\n"
    "Namensraum `runtime-refresh-no-run-closeout:<task>:<result>` geschrieben. Generische\n"
    "TaskSpec-Mutationen dürfen diesen Namensraum nicht belegen; beim Replay authentifiziert\n"
    "sein unveränderlicher Mutation-Receipt die ursprüngliche Closeout-Revision samt\n"
    "vollständiger Acceptance-Kapsel. Ein frei gesetztes `source`-Label ist dafür ausdrücklich\n"
    "keine Autorität.\n\n"
    + receipt_doc_anchor
)
docs = replace_once(docs, receipt_doc_anchor, receipt_doc_replacement, "receipt anchor docs")
docs_path.write_text(docs, encoding="utf-8")


# Self-delete helper files in the code commit.
Path(".github/scripts/tmp_pr2175_review_round2_fix.py").unlink()
Path(".github/workflows/tmp-pr2175-review-round2-fix.yml").unlink()

print(json.dumps({"migrated_registry_authorities": migrated}, indent=2))
