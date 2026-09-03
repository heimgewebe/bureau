from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from . import legacy, state_events
from .acceptance import AcceptanceContractError, validate_acceptance_contract
from .schema_validation import DocumentSchemaError, default_schema_set, validate_task_write

TASK_SPEC_SCHEMA_VERSION = 1
TASK_SPEC_EVENT_TYPE = "task-spec-revision-set"
TASK_SPEC_EVENT_SCHEMA_VERSION = state_events.EVENT_SCHEMA_VERSION
TASK_SPEC_PROJECTION_SCHEMA_VERSION = 1
RUNTIME_REFRESH_NO_RUN_CLOSEOUT_IDEMPOTENCY_PREFIX = "runtime-refresh-no-run-closeout:"
REPOSITORY_IDENTITY_REBIND_IDEMPOTENCY_PREFIX = "repository-identity-rebind:"
REPOSITORY_IDENTITY_REBIND_TERMINAL_STATES = frozenset({"verified", "cancelled", "superseded"})
REPOSITORY_IDENTITY_REBIND_ACTIVE_RUN_STATES = ("assigned", "running", "verifying")
_REPOSITORY_TOKEN_BEFORE_BOUNDARIES = frozenset(" \t\r\n'\"=:(,[{?#&;|<>")
_REPOSITORY_TOKEN_AFTER_BOUNDARIES = frozenset(" \t\r\n'\"/:),;]}?#&|<>")
_RESOURCE_ID_TOKEN_EXTRA_CHARS = frozenset("._-")
_URI_SCHEME_EXTRA_CHARS = frozenset("+-.")


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


def _acceptance_diagnostics(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        validate_acceptance_contract(spec)
    except AcceptanceContractError as exc:
        return json.loads(legacy.canonical_json(exc.diagnostics))
    return []


def _validate_repository_identity_rebind_parameters(
    *,
    old_resource_id: str,
    new_resource_id: str,
    old_repository_path: str,
    new_repository_path: str,
) -> None:
    for label, value in (
        ("old_resource_id", old_resource_id),
        ("new_resource_id", new_resource_id),
    ):
        if not isinstance(value, str) or not value.startswith("repo.") or len(value) > 240:
            raise TaskSpecError(f"repository identity rebind {label} is invalid")
    for label, value in (
        ("old_repository_path", old_repository_path),
        ("new_repository_path", new_repository_path),
    ):
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or value == "/"
            or value.endswith("/")
            or len(value) > 4096
        ):
            raise TaskSpecError(f"repository identity rebind {label} is invalid")
    if old_resource_id == new_resource_id:
        raise TaskSpecError("repository identity rebind resource ids must differ")
    if old_repository_path == new_repository_path:
        raise TaskSpecError("repository identity rebind repository paths must differ")


def _contains_resource_id_token(value: str, resource_id: str) -> bool:
    start = 0
    while True:
        index = value.find(resource_id, start)
        if index < 0:
            return False
        end = index + len(resource_id)
        before_ok = index == 0 or not (
            value[index - 1].isalnum()
            or value[index - 1] in _RESOURCE_ID_TOKEN_EXTRA_CHARS
        )
        after_ok = end == len(value) or not (
            value[end].isalnum() or value[end] in _RESOURCE_ID_TOKEN_EXTRA_CHARS
        )
        if before_ok and after_ok:
            return True
        start = index + 1


def _is_execution_binding_path(path: str) -> bool:
    return path == "/execution" or path.startswith("/execution/")


def _is_acceptance_evidence_path(path: str) -> bool:
    return path == "/acceptance" or path.startswith("/acceptance/")


def _is_uri_authority_path_boundary(value: str, index: int) -> bool:
    if index <= 0 or value[index] != "/":
        return False
    separator = value.rfind("://", 0, index)
    if separator <= 0:
        return False
    authority = value[separator + 3 : index]
    if not authority or any(
        character.isspace() or character in "/?#'\"" for character in authority
    ):
        return False
    scheme_start = separator
    while scheme_start > 0:
        character = value[scheme_start - 1]
        if character.isalnum() or character in _URI_SCHEME_EXTRA_CHARS:
            scheme_start -= 1
            continue
        break
    scheme = value[scheme_start:separator]
    if (
        not scheme
        or not scheme[0].isalpha()
        or any(
            not (character.isalnum() or character in _URI_SCHEME_EXTRA_CHARS)
            for character in scheme
        )
    ):
        return False
    return (
        scheme_start == 0
        or value[scheme_start - 1] in _REPOSITORY_TOKEN_BEFORE_BOUNDARIES
    )


def _contains_repository_path_token(
    value: str,
    repository_path: str,
    *,
    excluded_repository_path: str | None = None,
) -> bool:
    """Return whether one string contains the path as a technical token.

    Repository bindings may appear as plain paths or inside ``repo:``/``path:``
    resource keys and metadata strings.  Token boundaries deliberately exclude
    filename-style suffix characters, so rebinding ``/repos/app`` to
    ``/repos/app-new`` does not make the new path look like old residue.
    """

    start = 0
    while True:
        index = value.find(repository_path, start)
        if index < 0:
            return False
        end = index + len(repository_path)
        uri_prefix_ok = index >= 3 and value[index - 3 : index] == "://"
        uri_authority_ok = _is_uri_authority_path_boundary(value, index)
        before_ok = (
            index == 0
            or value[index - 1] in _REPOSITORY_TOKEN_BEFORE_BOUNDARIES
            or uri_prefix_ok
            or uri_authority_ok
        )
        after_ok = (
            end == len(value) or value[end] in _REPOSITORY_TOKEN_AFTER_BOUNDARIES
        )
        if before_ok and after_ok:
            if excluded_repository_path is not None and value.startswith(
                excluded_repository_path, index
            ):
                excluded_end = index + len(excluded_repository_path)
                excluded_after_ok = (
                    excluded_end == len(value)
                    or value[excluded_end] in _REPOSITORY_TOKEN_AFTER_BOUNDARIES
                )
                if excluded_after_ok:
                    start = index + 1
                    continue
            return True
        start = index + 1


def _old_repository_binding_residue(
    value: Any,
    *,
    old_resource_id: str,
    old_repository_path: str,
    new_repository_path: str,
    path: str = "",
) -> list[str]:
    if _is_acceptance_evidence_path(path):
        return []
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            item_path = f"{path}/{key}"
            execution_binding = _is_execution_binding_path(path)
            if isinstance(key, str) and (
                key == old_resource_id
                or (
                    execution_binding
                    and _contains_resource_id_token(key, old_resource_id)
                )
                or _contains_repository_path_token(
                    key,
                    old_repository_path,
                    excluded_repository_path=new_repository_path,
                )
            ):
                result.append(f"{item_path}#key")
            result.extend(
                _old_repository_binding_residue(
                    item,
                    old_resource_id=old_resource_id,
                    old_repository_path=old_repository_path,
                    new_repository_path=new_repository_path,
                    path=item_path,
                )
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(
                _old_repository_binding_residue(
                    item,
                    old_resource_id=old_resource_id,
                    old_repository_path=old_repository_path,
                    new_repository_path=new_repository_path,
                    path=f"{path}/{index}",
                )
            )
    elif isinstance(value, str) and (
        value == old_resource_id
        or (
            _is_execution_binding_path(path)
            and _contains_resource_id_token(value, old_resource_id)
        )
        or _contains_repository_path_token(
            value,
            old_repository_path,
            excluded_repository_path=new_repository_path,
        )
    ):
        result.append(path or "/")
    return result


def preview_repository_identity_rebind(
    spec: Mapping[str, Any],
    *,
    old_resource_id: str,
    new_resource_id: str,
    old_repository_path: str,
    new_repository_path: str,
) -> dict[str, Any]:
    """Return one invariant-checked repository-only TaskSpec transformation.

    This is deliberately *not* a compatibility write bypass.  It validates the
    Task document schema before and after the transformation, preserves the
    acceptance material and its diagnostics exactly, and only rewrites the
    canonical repository claim/path surfaces.
    """

    _validate_repository_identity_rebind_parameters(
        old_resource_id=old_resource_id,
        new_resource_id=new_resource_id,
        old_repository_path=old_repository_path,
        new_repository_path=new_repository_path,
    )
    canonical = _canonical_spec(spec)
    source = f"TaskSpec:{canonical['id']}"
    try:
        default_schema_set().validate("task", canonical, source)
    except DocumentSchemaError as exc:
        raise TaskSpecError(str(exc)) from exc

    acceptance_before = json.loads(
        legacy.canonical_json(canonical.get("acceptance", []))
    )
    diagnostics_before = _acceptance_diagnostics(canonical)
    material = json.loads(legacy.canonical_json(canonical))
    changed_paths: list[str] = []

    claims = material.get("claims")
    if not isinstance(claims, list):
        raise TaskSpecError("repository identity rebind TaskSpec claims must be a list")
    claim_changes = 0
    for index, claim in enumerate(claims):
        if isinstance(claim, dict) and claim.get("resource") == old_resource_id:
            claim["resource"] = new_resource_id
            changed_paths.append(f"/claims/{index}/resource")
            claim_changes += 1
    if claim_changes != 1:
        raise TaskSpecError(
            "repository identity rebind requires exactly one old repository claim"
        )

    execution = material.get("execution")
    if not isinstance(execution, dict):
        raise TaskSpecError("repository identity rebind TaskSpec execution must be an object")
    if execution.get("working_repository") == old_repository_path:
        execution["working_repository"] = new_repository_path
        changed_paths.append("/execution/working_repository")

    resources = execution.get("grabowski_resources")
    if resources is not None:
        if not isinstance(resources, list):
            raise TaskSpecError(
                "repository identity rebind grabowski_resources must be a list"
            )
        rewritten: list[Any] = []
        old_repo_key = f"repo:{old_repository_path}"
        new_repo_key = f"repo:{new_repository_path}"
        old_path_key = f"path:{old_repository_path}"
        new_path_key = f"path:{new_repository_path}"
        for index, item in enumerate(resources):
            if isinstance(item, str):
                old_repo_bound = item == old_repo_key or item.startswith(
                    old_repo_key + ":"
                )
                old_path_bound = item == old_path_key or (
                    item.startswith(old_path_key + "/")
                    and not (
                        new_repository_path.startswith(old_repository_path + "/")
                        and item.startswith(new_path_key + "/")
                    )
                )
                already_new_bound = (
                    item in (new_repo_key, new_path_key)
                    or item.startswith(new_repo_key + ":")
                    or item.startswith(new_path_key + "/")
                )
                if old_repo_bound:
                    item = new_repo_key + item[len(old_repo_key) :]
                    changed_paths.append(f"/execution/grabowski_resources/{index}")
                elif old_path_bound:
                    item = new_path_key + item[len(old_path_key) :]
                    changed_paths.append(f"/execution/grabowski_resources/{index}")
                elif already_new_bound:
                    rewritten.append(item)
                    continue
                elif old_repository_path in item:
                    if item.startswith(old_path_key + "/"):
                        item = new_path_key + item[len(old_path_key) :]
                    else:
                        raise TaskSpecError(
                            "repository identity rebind refuses an unscoped old repository path"
                        )
                    changed_paths.append(f"/execution/grabowski_resources/{index}")
            rewritten.append(item)
        execution["grabowski_resources"] = rewritten

    residue = _old_repository_binding_residue(
        material,
        old_resource_id=old_resource_id,
        old_repository_path=old_repository_path,
        new_repository_path=new_repository_path,
    )
    if residue:
        raise TaskSpecError(
            "repository identity rebind left old technical bindings at: "
            + ", ".join(sorted(residue))
        )

    try:
        default_schema_set().validate("task", material, source)
    except DocumentSchemaError as exc:
        raise TaskSpecError(str(exc)) from exc
    acceptance_after = json.loads(legacy.canonical_json(material.get("acceptance", [])))
    diagnostics_after = _acceptance_diagnostics(material)
    if acceptance_after != acceptance_before:
        raise TaskSpecError("repository identity rebind changed acceptance material")
    if diagnostics_after != diagnostics_before:
        raise TaskSpecError("repository identity rebind changed acceptance diagnostics")

    return {
        "spec": material,
        "spec_sha256": task_spec_digest(material),
        "changed_paths": changed_paths,
        "claim_changes": claim_changes,
        "working_repository_changes": int(
            "/execution/working_repository" in changed_paths
        ),
        "grabowski_resource_changes": sum(
            path.startswith("/execution/grabowski_resources/")
            for path in changed_paths
        ),
        "acceptance_sha256": legacy.sha256_json(acceptance_before),
        "acceptance_diagnostics_sha256": legacy.sha256_json(diagnostics_before),
    }


def repository_identity_rebind_idempotency_key(
    *,
    task_id: str,
    expected_revision: int,
    expected_spec_sha256: str,
    resulting_spec_sha256: str,
    old_resource_id: str,
    new_resource_id: str,
    old_repository_path: str,
    new_repository_path: str,
) -> str:
    binding = {
        "task_id": task_id,
        "expected_revision": expected_revision,
        "expected_spec_sha256": expected_spec_sha256,
        "resulting_spec_sha256": resulting_spec_sha256,
        "old_resource_id": old_resource_id,
        "new_resource_id": new_resource_id,
        "old_repository_path": old_repository_path,
        "new_repository_path": new_repository_path,
    }
    return (
        REPOSITORY_IDENTITY_REBIND_IDEMPOTENCY_PREFIX
        + task_id
        + ":"
        + legacy.sha256_json(binding)
    )


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
            FOREIGN KEY(task_id, resulting_revision)
                REFERENCES task_spec_revisions(task_id, revision)
        );
        CREATE INDEX IF NOT EXISTS task_spec_revision_digest
            ON task_spec_revisions(spec_sha256);
        """
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
    reserved_prefixes = (
        RUNTIME_REFRESH_NO_RUN_CLOSEOUT_IDEMPOTENCY_PREFIX,
        REPOSITORY_IDENTITY_REBIND_IDEMPOTENCY_PREFIX,
    )
    if idempotency_key.startswith(reserved_prefixes):
        raise TaskSpecError("TaskSpec idempotency namespace is reserved")
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
