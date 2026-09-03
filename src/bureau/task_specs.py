from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from . import legacy, state_events
from .acceptance import AcceptanceContractError
from .schema_validation import DocumentSchemaError, validate_task_write

TASK_SPEC_SCHEMA_VERSION = 1
TASK_SPEC_EVENT_TYPE = "task-spec-revision-set"
TASK_SPEC_EVENT_SCHEMA_VERSION = state_events.EVENT_SCHEMA_VERSION
TASK_SPEC_PROJECTION_SCHEMA_VERSION = 1
RUNTIME_REFRESH_NO_RUN_CLOSEOUT_IDEMPOTENCY_PREFIX = "runtime-refresh-no-run-closeout:"
RUNTIME_REFRESH_PROTECTED_PUBLICATION_ACTIVATION_IDEMPOTENCY_PREFIX = (
    "runtime-refresh-protected-publication-activation:"
)


class TaskSpecError(ValueError):
    """Fail-closed TaskSpec revision or replay error."""


def _canonical_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise TaskSpecError("TaskSpec must be an object")
    material = json.loads(legacy.canonical_json(dict(spec)))
    task_id = material.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise TaskSpecError("TaskSpec id must be a non-empty string")
    return material


def task_spec_digest(spec: Mapping[str, Any]) -> str:
    return legacy.sha256_json(_canonical_spec(spec))


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_spec_revisions(
            task_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            parent_revision INTEGER,
            spec_sha256 TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, revision)
        );
        CREATE TABLE IF NOT EXISTS task_specs(
            task_id TEXT PRIMARY KEY,
            current_revision INTEGER NOT NULL,
            spec_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_id, current_revision)
                REFERENCES task_spec_revisions(task_id, revision)
        );
        CREATE TABLE IF NOT EXISTS task_spec_mutations(
            idempotency_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            expected_revision INTEGER,
            requested_sha256 TEXT NOT NULL,
            resulting_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            activation_evidence_json TEXT,
            activation_evidence_sha256 TEXT,
            FOREIGN KEY(task_id, resulting_revision)
                REFERENCES task_spec_revisions(task_id, revision)
        );
        CREATE INDEX IF NOT EXISTS task_spec_revision_digest
            ON task_spec_revisions(spec_sha256);
        """
    )
    mutation_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(task_spec_mutations)")
    }
    for column in ("activation_evidence_json", "activation_evidence_sha256"):
        if column not in mutation_columns:
            connection.execute(
                f"ALTER TABLE task_spec_mutations ADD COLUMN {column} TEXT"
            )


def validate_schema(connection: sqlite3.Connection) -> None:
    required = {
        "task_spec_revisions": {
            "task_id", "revision", "parent_revision", "spec_sha256", "spec_json",
            "source", "created_at",
        },
        "task_specs": {"task_id", "current_revision", "spec_sha256", "updated_at"},
        "task_spec_mutations": {
            "idempotency_key", "task_id", "expected_revision", "requested_sha256",
            "resulting_revision", "created_at",
        },
    }
    missing: dict[str, list[str]] = {}
    for table, columns in required.items():
        observed = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        absent = sorted(columns - observed)
        if absent:
            missing[table] = absent
    if missing:
        raise TaskSpecError(f"TaskSpec schema drift: {missing}")


def _revision_row(connection: sqlite3.Connection, task_id: str, revision: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT task_id,revision,parent_revision,spec_sha256,spec_json,source,created_at "
        "FROM task_spec_revisions WHERE task_id=? AND revision=?",
        (task_id, revision),
    ).fetchone()
    if row is None:
        raise TaskSpecError(f"unknown TaskSpec revision: {task_id}@{revision}")
    return row


def _validated_row(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        spec = json.loads(str(row["spec_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TaskSpecError("TaskSpec revision JSON is invalid") from exc
    canonical = _canonical_spec(spec)
    task_id = str(row["task_id"])
    if canonical["id"] != task_id:
        raise TaskSpecError("TaskSpec revision id does not match row task_id")
    digest = task_spec_digest(canonical)
    if digest != row["spec_sha256"]:
        raise TaskSpecError("TaskSpec revision digest mismatch")
    revision = int(row["revision"])
    parent = row["parent_revision"]
    parent_revision = None if parent is None else int(parent)
    if revision < 1 or parent_revision != (None if revision == 1 else revision - 1):
        raise TaskSpecError("TaskSpec revision lineage is invalid")
    return {
        "task_id": task_id,
        "revision": revision,
        "parent_revision": parent_revision,
        "spec_sha256": digest,
        "spec": canonical,
        "source": str(row["source"]),
        "created_at": str(row["created_at"]),
    }


def get_current(connection: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    validate_schema(connection)
    pointer = connection.execute(
        "SELECT task_id,current_revision,spec_sha256,updated_at FROM task_specs WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if pointer is None:
        return None
    revision = _validated_row(_revision_row(connection, task_id, int(pointer["current_revision"])))
    if revision["spec_sha256"] != pointer["spec_sha256"]:
        raise TaskSpecError("TaskSpec current pointer digest mismatch")
    return {**revision, "updated_at": str(pointer["updated_at"])}


def get_revision(connection: sqlite3.Connection, task_id: str, revision: int) -> dict[str, Any]:
    validate_schema(connection)
    return _validated_row(_revision_row(connection, task_id, revision))


def get_by_digest(
    connection: sqlite3.Connection, task_id: str, spec_sha256: str
) -> dict[str, Any] | None:
    validate_schema(connection)
    row = connection.execute(
        "SELECT task_id,revision,parent_revision,spec_sha256,spec_json,source,created_at "
        "FROM task_spec_revisions WHERE task_id=? AND spec_sha256=? "
        "ORDER BY revision DESC LIMIT 1",
        (task_id, spec_sha256),
    ).fetchone()
    if row is None:
        return None
    return _validated_row(row)



def get_mutation_receipt(
    connection: sqlite3.Connection, idempotency_key: str
) -> dict[str, Any] | None:
    validate_schema(connection)
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise TaskSpecError("TaskSpec idempotency key must be non-empty")
    mutation_columns = {
        str(item["name"])
        for item in connection.execute("PRAGMA table_info(task_spec_mutations)")
    }
    evidence_columns_present = {
        "activation_evidence_json",
        "activation_evidence_sha256",
    }.issubset(mutation_columns)
    evidence_select = (
        ",activation_evidence_json,activation_evidence_sha256"
        if evidence_columns_present
        else ""
    )
    row = connection.execute(
        "SELECT idempotency_key,task_id,expected_revision,requested_sha256,"
        f"resulting_revision,created_at{evidence_select} "
        "FROM task_spec_mutations WHERE idempotency_key=?",
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
    receipt = {
        "idempotency_key": idempotency_key,
        "task_id": task_id,
        "expected_revision": expected_revision,
        "requested_sha256": requested_sha256,
        "resulting_revision": resulting_revision,
        "created_at": created_at,
        "resulting_task_spec": resulting_task_spec,
    }
    if evidence_columns_present:
        raw_evidence = row["activation_evidence_json"]
        evidence_sha256 = row["activation_evidence_sha256"]
        if (raw_evidence is None) != (evidence_sha256 is None):
            raise TaskSpecError("TaskSpec mutation activation evidence is incomplete")
        if raw_evidence is not None:
            if not isinstance(raw_evidence, str) or not isinstance(evidence_sha256, str):
                raise TaskSpecError("TaskSpec mutation activation evidence is invalid")
            try:
                evidence = json.loads(raw_evidence)
            except json.JSONDecodeError as exc:
                raise TaskSpecError(
                    "TaskSpec mutation activation evidence JSON is invalid"
                ) from exc
            if not isinstance(evidence, dict):
                raise TaskSpecError("TaskSpec mutation activation evidence must be an object")
            payload = dict(evidence)
            observed = payload.pop("evidence_sha256", None)
            expected = hashlib.sha256(
                (legacy.canonical_json(payload) + "\n").encode()
            ).hexdigest()
            if observed != evidence_sha256 or expected != evidence_sha256:
                raise TaskSpecError("TaskSpec mutation activation evidence digest mismatch")
            receipt["activation_evidence"] = evidence
            receipt["activation_evidence_sha256"] = evidence_sha256
    return receipt

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
    validate_schema(connection)
    reserved_receipt = connection.execute(
        "SELECT 1 FROM task_spec_mutations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if reserved_receipt is None:
        current = get_current(connection, canonical["id"])
        if current is not None and current["spec_sha256"] == task_spec_digest(canonical):
            raise TaskSpecError(
                "runtime-refresh no-run closeout must create its authenticated TaskSpec revision"
            )
    return _put_validated_material(
        connection,
        canonical,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        source="runtime-refresh-no-run-closeout",
    )


def _validate_runtime_refresh_protected_publication_activation_mutation(
    connection: sqlite3.Connection,
    spec: Mapping[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int | None,
    activation_observation: Mapping[str, Any] | None,
    activation_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    canonical = _canonical_spec(spec)
    metadata = canonical.get("metadata")
    publication = (
        metadata.get("protected_publication_adoption")
        if isinstance(metadata, Mapping)
        else None
    )
    activation = (
        metadata.get("post_publication_activation")
        if isinstance(metadata, Mapping)
        else None
    )
    embedded_observation = (
        metadata.get("protected_publication_activation_observation")
        if isinstance(metadata, Mapping)
        else None
    )
    observation = activation_observation
    evidence = activation_evidence
    evidence_payload = dict(evidence) if isinstance(evidence, Mapping) else {}
    evidence_digest = evidence_payload.pop("evidence_sha256", None)
    expected_evidence_digest = hashlib.sha256(
        (legacy.canonical_json(evidence_payload) + "\n").encode()
    ).hexdigest()
    installed_validation = (
        evidence.get("installed_runtime_validation")
        if isinstance(evidence, Mapping)
        else None
    )
    installed_validation_payload = (
        dict(installed_validation) if isinstance(installed_validation, Mapping) else {}
    )
    installed_validation_digest = installed_validation_payload.pop(
        "validation_sha256", None
    )
    expected_installed_validation_digest = hashlib.sha256(
        (legacy.canonical_json(installed_validation_payload) + "\n").encode()
    ).hexdigest()
    observation_payload = dict(observation) if isinstance(observation, Mapping) else {}
    observation_digest = observation_payload.pop("observation_sha256", None)
    expected_observation_digest = hashlib.sha256(
        (legacy.canonical_json(observation_payload) + "\n").encode()
    ).hexdigest()
    target_payload = {
        key: observation.get(key) if isinstance(observation, Mapping) else None
        for key in (
            "repository",
            "main_commit",
            "pull_request",
            "merged_at",
            "required_checks",
            "check_summary",
            "deployed_source_commit",
            "deployed_manifest_sha256",
            "lag_commits",
            "scheduler_target_state",
        )
    }
    expected_target_sha256 = hashlib.sha256(
        (legacy.canonical_json(target_payload) + "\n").encode()
    ).hexdigest()
    source_identity = (
        observation.get("runtime_source_identity")
        if isinstance(observation, Mapping)
        else None
    )
    source_ancestry = (
        observation.get("source_ancestry")
        if isinstance(observation, Mapping)
        else None
    )
    merge_commit = (
        publication.get("publication_merge_commit")
        if isinstance(publication, Mapping)
        else None
    )
    publication_pr = (
        publication.get("publication_pr") if isinstance(publication, Mapping) else None
    )
    activation_spec_sha256 = task_spec_digest(canonical)
    if (
        canonical.get("state") != "ready"
        or not isinstance(activation, Mapping)
        or activation.get("initial_state") != "planned"
        or activation.get("activation_state") != "ready"
        or activation.get("required_activation_source")
        != "runtime-refresh-protected-publication-activation"
        or embedded_observation is not None
        or not isinstance(publication, Mapping)
        or not isinstance(publication_pr, int)
        or isinstance(publication_pr, bool)
        or publication_pr < 1
        or not isinstance(evidence, Mapping)
        or evidence.get("schema_version") != 1
        or evidence.get("kind")
        != "bureau_runtime_refresh_protected_publication_activation_evidence"
        or evidence.get("task_id") != canonical.get("id")
        or evidence.get("adoption_revision") != expected_revision
        or evidence.get("activation_spec_sha256") != activation_spec_sha256
        or evidence.get("idempotency_key") != idempotency_key
        or evidence.get("publication_pr") != publication_pr
        or evidence.get("publication_merge_commit") != merge_commit
        or evidence.get("target_main_commit")
        != (observation.get("main_commit") if isinstance(observation, Mapping) else None)
        or not isinstance(evidence.get("task_file_sha256"), str)
        or len(str(evidence.get("task_file_sha256"))) != 64
        or any(
            character not in "0123456789abcdef"
            for character in str(evidence.get("task_file_sha256"))
        )
        or evidence.get("target_sha256")
        != (observation.get("target_sha256") if isinstance(observation, Mapping) else None)
        or evidence.get("observation_sha256")
        != (observation.get("observation_sha256") if isinstance(observation, Mapping) else None)
        or evidence.get("observation") != observation
        or evidence_digest != expected_evidence_digest
        or not isinstance(installed_validation, Mapping)
        or installed_validation.get("kind")
        != "bureau_runtime_refresh_installed_activation_candidate_validation"
        or installed_validation.get("task_id") != canonical.get("id")
        or installed_validation.get("candidate_spec_sha256")
        != activation_spec_sha256
        or installed_validation.get("installed_source_commit")
        != (
            observation.get("deployed_source_commit")
            if isinstance(observation, Mapping)
            else None
        )
        or installed_validation.get("deployment_manifest_sha256")
        != (
            observation.get("deployed_manifest_sha256")
            if isinstance(observation, Mapping)
            else None
        )
        or evidence.get("installed_runtime_validation_sha256")
        != installed_validation_digest
        or installed_validation_digest != expected_installed_validation_digest
        or not isinstance(publication, Mapping)
        or not isinstance(merge_commit, str)
        or len(merge_commit) != 40
        or any(character not in "0123456789abcdef" for character in merge_commit)
        or not isinstance(observation, Mapping)
        or observation.get("schema_version") != 1
        or observation.get("kind") != "bureau_runtime_refresh_observation"
        or observation.get("status") not in {"candidate", "alert"}
        or not isinstance(observation.get("observed_at"), str)
        or not isinstance(observation.get("main_commit"), str)
        or len(str(observation.get("main_commit"))) != 40
        or any(
            character not in "0123456789abcdef"
            for character in str(observation.get("main_commit"))
        )
        or not isinstance(observation.get("target_sha256"), str)
        or observation.get("target_sha256") != expected_target_sha256
        or observation_digest != expected_observation_digest
        or not isinstance(source_identity, Mapping)
        or source_identity.get("schema_version") != 1
        or source_identity.get("status") != "proven"
        or source_identity.get("deployed_source_commit")
        != observation.get("deployed_source_commit")
        or source_identity.get("registry_source_commit")
        != observation.get("deployed_source_commit")
        or source_identity.get("registry_reasons") != []
        or not isinstance(source_ancestry, Mapping)
        or source_ancestry.get("schema_version") != 1
        or source_ancestry.get("status") != "proven"
        or source_ancestry.get("deployed_source_commit")
        != observation.get("deployed_source_commit")
        or source_ancestry.get("main_commit") != observation.get("main_commit")
    ):
        raise TaskSpecError(
            "runtime-refresh protected-publication activation mutation contract is invalid"
        )
    validate_schema(connection)
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 1
    ):
        raise TaskSpecError(
            "runtime-refresh protected-publication activation requires exact planned CAS baseline"
        )
    replay = connection.execute(
        "SELECT task_id,expected_revision,requested_sha256,resulting_revision "
        "FROM task_spec_mutations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if replay is not None:
        baseline = get_revision(connection, canonical["id"], expected_revision)
        expected_key = (
            f"{RUNTIME_REFRESH_PROTECTED_PUBLICATION_ACTIVATION_IDEMPOTENCY_PREFIX}"
            f"{canonical['id']}:{merge_commit}:{baseline['spec_sha256']}"
        )
        digest = task_spec_digest(canonical)
        resulting_revision = replay["resulting_revision"]
        result = (
            get_revision(connection, canonical["id"], int(resulting_revision))
            if isinstance(resulting_revision, int)
            else None
        )
        if (
            idempotency_key != expected_key
            or not isinstance(baseline.get("spec"), Mapping)
            or baseline["spec"].get("state") != "planned"
            or replay["task_id"] != canonical["id"]
            or replay["expected_revision"] != expected_revision
            or replay["requested_sha256"] != digest
            or evidence.get("adoption_spec_sha256") != baseline.get("spec_sha256")
            or resulting_revision != expected_revision + 1
            or not isinstance(result, dict)
            or result.get("spec_sha256") != digest
            or result.get("source")
            != "runtime-refresh-protected-publication-activation"
            or result.get("spec") != canonical
        ):
            raise TaskSpecError(
                "runtime-refresh protected-publication activation replay binding is invalid"
            )
        return canonical

    try:
        observed_at = legacy.parse_time(str(observation.get("observed_at")))
        cas_at = legacy.parse_time(legacy.utc_now())
    except (TypeError, ValueError) as exc:
        raise TaskSpecError(
            "runtime-refresh protected-publication activation observation timestamp is invalid"
        ) from exc
    age_seconds = int((cas_at - observed_at).total_seconds())
    if age_seconds < -30 or age_seconds > 300:
        raise TaskSpecError(
            "runtime-refresh protected-publication activation observation is not fresh at CAS"
        )
    current = get_current(connection, canonical["id"])
    if (
        current is None
        or current.get("revision") != expected_revision
        or not isinstance(current.get("spec"), Mapping)
        or current["spec"].get("state") != "planned"
    ):
        raise TaskSpecError(
            "runtime-refresh protected-publication activation requires exact planned CAS baseline"
        )
    expected_key = (
        f"{RUNTIME_REFRESH_PROTECTED_PUBLICATION_ACTIVATION_IDEMPOTENCY_PREFIX}"
        f"{canonical['id']}:{merge_commit}:{current['spec_sha256']}"
    )
    if idempotency_key != expected_key:
        raise TaskSpecError(
            "runtime-refresh protected-publication activation idempotency binding is invalid"
        )
    if evidence.get("adoption_spec_sha256") != current.get("spec_sha256"):
        raise TaskSpecError(
            "runtime-refresh protected-publication activation evidence baseline is invalid"
        )
    return canonical


def put_runtime_refresh_protected_publication_activation(
    connection: sqlite3.Connection,
    spec: Mapping[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int | None,
    activation_observation: Mapping[str, Any] | None,
    activation_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    canonical = _validate_runtime_refresh_protected_publication_activation_mutation(
        connection,
        spec,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        activation_observation=activation_observation,
        activation_evidence=activation_evidence,
    )
    try:
        validate_task_write(canonical, f"TaskSpec:{canonical['id']}")
    except (DocumentSchemaError, AcceptanceContractError) as exc:
        raise TaskSpecError(str(exc)) from exc
    if not isinstance(activation_evidence, Mapping):
        raise TaskSpecError(
            "runtime-refresh protected-publication activation evidence is required"
        )
    evidence = dict(activation_evidence)
    evidence_sha256 = evidence.get("evidence_sha256")
    if not isinstance(evidence_sha256, str) or len(evidence_sha256) != 64:
        raise TaskSpecError(
            "runtime-refresh protected-publication activation evidence digest is invalid"
        )
    evidence_json = legacy.canonical_json(evidence)
    reserved_receipt = connection.execute(
        "SELECT activation_evidence_json,activation_evidence_sha256 "
        "FROM task_spec_mutations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if reserved_receipt is None:
        current = get_current(connection, canonical["id"])
        if current is not None and current["spec_sha256"] == task_spec_digest(canonical):
            raise TaskSpecError(
                "runtime-refresh protected-publication activation must create its "
                "authenticated TaskSpec revision"
            )
    else:
        if (
            reserved_receipt["activation_evidence_json"] != evidence_json
            or reserved_receipt["activation_evidence_sha256"] != evidence_sha256
        ):
            raise TaskSpecError(
                "runtime-refresh protected-publication activation replay evidence differs"
            )
    result = _put_validated_material(
        connection,
        canonical,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        source="runtime-refresh-protected-publication-activation",
    )
    if reserved_receipt is None:
        updated = connection.execute(
            "UPDATE task_spec_mutations SET "
            "activation_evidence_json=?,activation_evidence_sha256=? "
            "WHERE idempotency_key=? AND activation_evidence_json IS NULL "
            "AND activation_evidence_sha256 IS NULL",
            (evidence_json, evidence_sha256, idempotency_key),
        )
        if updated.rowcount != 1:
            raise TaskSpecError(
                "runtime-refresh protected-publication activation evidence was not "
                "stored atomically"
            )
    receipt = get_mutation_receipt(connection, idempotency_key)
    if (
        not isinstance(receipt, dict)
        or receipt.get("activation_evidence") != evidence
        or receipt.get("activation_evidence_sha256") != evidence_sha256
        or receipt.get("resulting_revision") != result.get("revision")
        or receipt.get("requested_sha256") != result.get("spec_sha256")
    ):
        raise TaskSpecError(
            "runtime-refresh protected-publication activation evidence readback failed"
        )
    return result

def current_projection(connection: sqlite3.Connection) -> dict[str, Any]:
    validate_schema(connection)
    tasks: dict[str, dict[str, Any]] = {}
    for pointer in connection.execute(
        "SELECT task_id,current_revision,spec_sha256,updated_at FROM task_specs ORDER BY task_id"
    ):
        item = get_current(connection, str(pointer["task_id"]))
        assert item is not None
        tasks[item["task_id"]] = {
            "revision": item["revision"],
            "spec_sha256": item["spec_sha256"],
            "spec": item["spec"],
        }
    return {"schema_version": TASK_SPEC_PROJECTION_SCHEMA_VERSION, "tasks": tasks}


def projection_root(projection: Mapping[str, Any]) -> str:
    if not isinstance(projection, Mapping) or set(projection) != {"schema_version", "tasks"}:
        raise TaskSpecError("TaskSpec projection fields are not exact")
    if projection.get("schema_version") != TASK_SPEC_PROJECTION_SCHEMA_VERSION:
        raise TaskSpecError("TaskSpec projection schema version is unsupported")
    if not isinstance(projection.get("tasks"), Mapping):
        raise TaskSpecError("TaskSpec projection tasks must be an object")
    return legacy.sha256_json(dict(projection))


def _event_payload(
    *,
    task_id: str,
    revision: int,
    parent_revision: int | None,
    digest: str,
    spec: Mapping[str, Any],
    source: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "schema_version": TASK_SPEC_SCHEMA_VERSION,
        "task_id": task_id,
        "revision": revision,
        "parent_revision": parent_revision,
        "spec_sha256": digest,
        "spec": _canonical_spec(spec),
        "source": source,
        "idempotency_key": idempotency_key,
    }


def validate_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "task_id", "revision", "parent_revision", "spec_sha256",
        "spec", "source", "idempotency_key",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise TaskSpecError("TaskSpec event fields are not exact")
    if payload.get("schema_version") != TASK_SPEC_SCHEMA_VERSION:
        raise TaskSpecError("TaskSpec event payload schema version is unsupported")
    task_id = payload.get("task_id")
    revision = payload.get("revision")
    parent = payload.get("parent_revision")
    source = payload.get("source")
    key = payload.get("idempotency_key")
    if not isinstance(task_id, str) or not task_id:
        raise TaskSpecError("TaskSpec event task_id is invalid")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise TaskSpecError("TaskSpec event revision is invalid")
    if parent != (None if revision == 1 else revision - 1):
        raise TaskSpecError("TaskSpec event parent revision is invalid")
    if not isinstance(source, str) or not source or not isinstance(key, str) or not key:
        raise TaskSpecError("TaskSpec event source/idempotency binding is invalid")
    spec = _canonical_spec(payload.get("spec"))
    if spec["id"] != task_id:
        raise TaskSpecError("TaskSpec event id does not match task_id")
    digest = task_spec_digest(spec)
    if payload.get("spec_sha256") != digest:
        raise TaskSpecError("TaskSpec event digest mismatch")
    return _event_payload(
        task_id=task_id,
        revision=revision,
        parent_revision=parent,
        digest=digest,
        spec=spec,
        source=source,
        idempotency_key=key,
    )


def _insert_event(connection: sqlite3.Connection, payload: Mapping[str, Any]) -> None:
    checked = validate_event_payload(payload)
    connection.execute(
        "INSERT INTO events(run_id,event_type,event_schema_version,payload_json,created_at) "
        "VALUES(NULL,?,?,?,?)",
        (
            TASK_SPEC_EVENT_TYPE,
            TASK_SPEC_EVENT_SCHEMA_VERSION,
            legacy.canonical_json(checked),
            legacy.utc_now(),
        ),
    )


def _put_validated_material(
    connection: sqlite3.Connection,
    canonical: dict[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int | None,
    source: str,
) -> dict[str, Any]:
    """Apply already-selected material inside the caller's IMMEDIATE transaction."""
    validate_schema(connection)
    task_id = canonical["id"]
    digest = task_spec_digest(canonical)
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise TaskSpecError("TaskSpec idempotency key must be non-empty")
    if not isinstance(source, str) or not source:
        raise TaskSpecError("TaskSpec mutation source must be non-empty")
    if expected_revision is not None and (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 1
    ):
        raise TaskSpecError("expected TaskSpec revision must be a positive integer or null")

    replay = connection.execute(
        "SELECT task_id,expected_revision,requested_sha256,resulting_revision "
        "FROM task_spec_mutations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if replay is not None:
        if (
            replay["task_id"] != task_id
            or replay["expected_revision"] != expected_revision
            or replay["requested_sha256"] != digest
        ):
            raise TaskSpecError("TaskSpec idempotency key is bound to another mutation")
        current = get_current(connection, task_id)
        if current is None or current["revision"] < int(replay["resulting_revision"]):
            raise TaskSpecError("TaskSpec idempotency receipt references an unknown revision")
        result = _validated_row(
            _revision_row(connection, task_id, int(replay["resulting_revision"]))
        )
        if result["spec_sha256"] != digest:
            raise TaskSpecError("TaskSpec idempotency revision digest mismatch")
        return {**result, "idempotent_replay": True, "changed": False}

    current = get_current(connection, task_id)
    current_revision = None if current is None else int(current["revision"])
    if current_revision != expected_revision:
        raise TaskSpecError(
            f"stale TaskSpec revision baseline for {task_id}: "
            f"expected {expected_revision!r}, current {current_revision!r}"
        )
    if current is not None and current["spec_sha256"] == digest:
        connection.execute(
            "INSERT INTO task_spec_mutations("
            "idempotency_key,task_id,expected_revision,requested_sha256,resulting_revision,created_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                idempotency_key,
                task_id,
                expected_revision,
                digest,
                current_revision,
                legacy.utc_now(),
            ),
        )
        return {**current, "idempotent_replay": False, "changed": False}

    revision = 1 if current_revision is None else current_revision + 1
    parent = current_revision
    now = legacy.utc_now()
    connection.execute(
        "INSERT INTO task_spec_revisions("
        "task_id,revision,parent_revision,spec_sha256,spec_json,source,created_at"
        ") VALUES(?,?,?,?,?,?,?)",
        (task_id, revision, parent, digest, legacy.canonical_json(canonical), source, now),
    )
    connection.execute(
        "INSERT INTO task_specs(task_id,current_revision,spec_sha256,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET "
        "current_revision=excluded.current_revision,spec_sha256=excluded.spec_sha256,updated_at=excluded.updated_at",
        (task_id, revision, digest, now),
    )
    connection.execute(
        "INSERT INTO task_spec_mutations("
        "idempotency_key,task_id,expected_revision,requested_sha256,resulting_revision,created_at"
        ") VALUES(?,?,?,?,?,?)",
        (idempotency_key, task_id, expected_revision, digest, revision, now),
    )
    _insert_event(
        connection,
        _event_payload(
            task_id=task_id,
            revision=revision,
            parent_revision=parent,
            digest=digest,
            spec=canonical,
            source=source,
            idempotency_key=idempotency_key,
        ),
    )
    return {
        "task_id": task_id,
        "revision": revision,
        "parent_revision": parent,
        "spec_sha256": digest,
        "spec": canonical,
        "source": source,
        "created_at": now,
        "idempotent_replay": False,
        "changed": True,
    }


def put(
    connection: sqlite3.Connection,
    spec: Mapping[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int | None,
    source: str,
) -> dict[str, Any]:
    """Apply one strictly validated TaskSpec CAS mutation.

    ``source`` is provenance only and can never select a compatibility bypass.
    """

    canonical = _canonical_spec(spec)
    if idempotency_key.startswith(RUNTIME_REFRESH_NO_RUN_CLOSEOUT_IDEMPOTENCY_PREFIX):
        raise TaskSpecError(
            "runtime-refresh no-run closeout idempotency namespace is reserved"
        )
    if idempotency_key.startswith(
        RUNTIME_REFRESH_PROTECTED_PUBLICATION_ACTIVATION_IDEMPOTENCY_PREFIX
    ):
        raise TaskSpecError(
            "runtime-refresh protected-publication activation idempotency namespace is reserved"
        )
    try:
        validate_task_write(canonical, f"TaskSpec:{canonical['id']}")
    except (DocumentSchemaError, AcceptanceContractError) as exc:
        raise TaskSpecError(str(exc)) from exc
    return _put_validated_material(
        connection,
        canonical,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        source=source,
    )


def _put_legacy_registry_import(
    connection: sqlite3.Connection,
    spec: Mapping[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int | None,
    source: str,
) -> dict[str, Any]:
    """Import an exact historical Git projection without revising its semantics."""

    return _put_validated_material(
        connection,
        _canonical_spec(spec),
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        source=source,
    )


def import_registry(connection: sqlite3.Connection, registry: Any) -> dict[str, Any]:
    """Idempotently import exact Git TaskSpecs without changing their semantics."""
    imported = 0
    unchanged = 0
    for task_id in sorted(registry.tasks):
        task = registry.tasks[task_id]
        current = get_current(connection, task_id)
        expected = None if current is None else int(current["revision"])
        digest = task_spec_digest(task.raw)
        if current is not None:
            if current["spec_sha256"] != digest:
                raise TaskSpecError(
                    f"legacy TaskSpec divergence for {task_id}: "
                    "StateStore differs from Git projection"
                )
            unchanged += 1
            continue
        result = _put_legacy_registry_import(
            connection,
            task.raw,
            idempotency_key=f"legacy-import:{task_id}:{digest}",
            expected_revision=expected,
            source="legacy-git-import",
        )
        if result["changed"]:
            imported += 1
        else:
            unchanged += 1
    return {"imported": imported, "unchanged": unchanged, "total": len(registry.tasks)}


def seed_missing_registry(
    connection: sqlite3.Connection, registry: Any
) -> dict[str, Any]:
    """Import only missing Git TaskSpecs while preserving authoritative StateStore revisions.

    This is the transition helper for registering one reviewed TaskSpec. Existing
    StateStore specs are validated by ``get_current`` but are never overwritten or
    rejected merely because the Git projection is older. Use ``import_registry``
    when an explicit strict Git/StateStore equality check is intended.
    """
    imported = 0
    unchanged = 0
    divergent_preserved: list[str] = []
    for task_id in sorted(registry.tasks):
        task = registry.tasks[task_id]
        current = get_current(connection, task_id)
        digest = task_spec_digest(task.raw)
        if current is not None:
            if current["spec_sha256"] == digest:
                unchanged += 1
            else:
                divergent_preserved.append(task_id)
            continue
        result = _put_legacy_registry_import(
            connection,
            task.raw,
            idempotency_key=f"legacy-seed:{task_id}:{digest}",
            expected_revision=None,
            source="legacy-git-seed",
        )
        if result["changed"]:
            imported += 1
        else:
            unchanged += 1
    sample_limit = 20
    return {
        "imported": imported,
        "unchanged": unchanged,
        "divergent_preserved_count": len(divergent_preserved),
        "divergent_preserved_sample": divergent_preserved[:sample_limit],
        "divergent_preserved_truncated": len(divergent_preserved) > sample_limit,
        "total": len(registry.tasks),
        "mode": "seed-missing-preserve-state-store",
    }


def seed_missing_registry_task(
    connection: sqlite3.Connection,
    registry: Any,
    task_id: str,
) -> dict[str, Any]:
    """Import exactly one missing Git TaskSpec and refuse any divergence.

    Unlike ``seed_missing_registry`` this helper never iterates over the Registry.
    It exists for tightly scoped bootstrap callers that have independently proven
    authority for one exact task. Existing StateStore truth is either an exact
    digest match (idempotent replay) or a hard error; it is never overwritten.
    """
    if not isinstance(task_id, str) or not task_id:
        raise TaskSpecError("exact Registry TaskSpec id must be non-empty")
    tasks = getattr(registry, "tasks", None)
    if not isinstance(tasks, Mapping) or task_id not in tasks:
        raise TaskSpecError(f"unknown exact Registry TaskSpec: {task_id}")
    raw = getattr(tasks[task_id], "raw", None)
    canonical = _canonical_spec(raw)
    if canonical["id"] != task_id:
        raise TaskSpecError("exact Registry TaskSpec id does not match requested task")

    digest = task_spec_digest(canonical)
    current = get_current(connection, task_id)
    if current is not None:
        if current["spec_sha256"] != digest:
            raise TaskSpecError(
                f"exact TaskSpec divergence for {task_id}: "
                "StateStore differs from Git projection"
            )
        return {
            "mode": "seed-exact-missing-preserve-state-store",
            "task_id": task_id,
            "revision": current["revision"],
            "spec_sha256": digest,
            "changed": False,
            "idempotent_replay": True,
        }

    result = _put_legacy_registry_import(
        connection,
        canonical,
        idempotency_key=f"legacy-seed-exact:{task_id}:{digest}",
        expected_revision=None,
        source="legacy-git-exact-seed",
    )
    return {
        "mode": "seed-exact-missing-preserve-state-store",
        "task_id": task_id,
        "revision": result["revision"],
        "spec_sha256": result["spec_sha256"],
        "changed": result["changed"],
        "idempotent_replay": result["idempotent_replay"],
    }


def split_event_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    base: list[Mapping[str, Any]] = []
    task_spec_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if row.get("event_type") == TASK_SPEC_EVENT_TYPE:
            task_spec_rows.append(row)
        else:
            base.append(row)
    return base, task_spec_rows


def replay(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    tasks: dict[str, dict[str, Any]] = {}
    event_count = 0
    last_event_id = -1
    idempotency: set[str] = set()
    for row in rows:
        try:
            event_id = int(row["event_id"])
            version = int(row["event_schema_version"])
            event_type = str(row["event_type"])
            payload = json.loads(str(row["payload_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskSpecError("TaskSpec event row is malformed") from exc
        if event_id <= last_event_id:
            raise TaskSpecError("TaskSpec event ordering is not strictly increasing")
        last_event_id = event_id
        if event_type != TASK_SPEC_EVENT_TYPE:
            raise TaskSpecError("unexpected event in TaskSpec replay")
        if version != TASK_SPEC_EVENT_SCHEMA_VERSION:
            raise TaskSpecError(f"unsupported TaskSpec event schema version: {version}")
        checked = validate_event_payload(payload)
        key = checked["idempotency_key"]
        if key in idempotency:
            raise TaskSpecError("duplicate TaskSpec idempotency key in event journal")
        idempotency.add(key)
        task_id = checked["task_id"]
        current = tasks.get(task_id)
        expected_parent = None if current is None else current["revision"]
        if checked["parent_revision"] != expected_parent:
            raise TaskSpecError("TaskSpec replay revision lineage mismatch")
        tasks[task_id] = {
            "revision": checked["revision"],
            "spec_sha256": checked["spec_sha256"],
            "spec": checked["spec"],
        }
        event_count += 1
    projection = {"schema_version": TASK_SPEC_PROJECTION_SCHEMA_VERSION, "tasks": tasks}
    return {
        "schema_version": TASK_SPEC_PROJECTION_SCHEMA_VERSION,
        "projection": projection,
        "root_sha256": projection_root(projection),
        "event_count": event_count,
        "last_event_id": last_event_id,
    }


def verify_replay(
    connection: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    current = current_projection(connection)
    replayed = replay(rows)
    current_root = projection_root(current)
    if replayed["root_sha256"] != current_root:
        raise TaskSpecError(
            "replayed TaskSpec projection does not match current StateStore projection"
        )
    return {**replayed, "current_root_sha256": current_root, "matches_current": True}
