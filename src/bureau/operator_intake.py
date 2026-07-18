from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import legacy
from .approval import (
    approval_decision,
    require_approval,
    reviewed_plan_approval,
    task_approval_contract,
)
from .core import Registry, StateError, StateStore
from .lease_contract import (
    BUREAU_REGISTRY_PUBLICATION_GATE_KEY,
    BUREAU_REPOSITORY_ROOT,
)
from .live_register import (
    ACTIVE_LIVE_STATUSES,
    candidate_records,
    current_candidate_record,
    current_candidate_records,
    live_register_record,
)
from .runtime_refresh import (
    DEFAULT_GRABOWSKI_RESOURCE_DB,
    RuntimeRefreshError,
    validate_live_lease_binding,
)

OPERATOR_INTAKE_SCHEMA_VERSION = 1
MAX_SIMILARITY_RESULTS = 5
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_GENERIC_ACCEPTANCE_IDS = {"source-event-bound", "reviewed-before-effect"}


class OperatorIntakeError(StateError):
    """Typed operator-intake failure with explicit retry and readback semantics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        effect_started: bool = False,
        ambiguity: bool = False,
        required_readback: Sequence[str] = (),
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.effect_started = effect_started
        self.ambiguity = ambiguity
        self.required_readback = tuple(required_readback)
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
            "kind": "bureau_operator_intake_failure",
            "status": "failed",
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "effect_started": self.effect_started,
            "ambiguity": self.ambiguity,
            "required_readback": list(self.required_readback),
            "details": self.details,
            "does_not_establish": ["safe_retry", "effect_absence"],
        }


def read_json_object_file(
    path: str | Path,
    *,
    field: str,
) -> dict[str, Any]:
    """Read one operator transport object with stable machine failure semantics."""
    target = Path(path).expanduser()
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise OperatorIntakeError(
            f"{field}-read-failed",
            f"cannot read {field} file {target}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(target)},
        ) from exc
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            f"{field}-json-invalid",
            f"cannot parse {field} JSON from {target}: {exc}",
            details={"path": str(target)},
        ) from exc
    if not isinstance(value, dict):
        raise OperatorIntakeError(
            f"{field}-object-required",
            f"{field} JSON must be an object",
            details={"path": str(target)},
        )
    return value


class TaskPublisher(Protocol):
    def publish(
        self,
        *,
        registry: Registry,
        plan: dict[str, Any],
        workspace_root: Path,
        assert_plan_unchanged: Callable[[], None],
    ) -> dict[str, Any]: ...


def _checked_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    required: bool = True,
) -> str | None:
    if value is None:
        if required:
            raise OperatorIntakeError("missing-field", f"{field} is required")
        return None
    if not isinstance(value, str):
        raise OperatorIntakeError("invalid-field", f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        if required:
            raise OperatorIntakeError("empty-field", f"{field} must not be empty")
        return None
    if len(normalized) > maximum:
        raise OperatorIntakeError("field-too-long", f"{field} must be at most {maximum} characters")
    return normalized


def _checked_source_sha(value: Any) -> str | None:
    normalized = _checked_text(value, field="source_sha256", maximum=64, required=False)
    if normalized is not None and not _SOURCE_SHA_RE.fullmatch(normalized):
        raise OperatorIntakeError(
            "source-digest-invalid", "source_sha256 must be a lowercase SHA-256 digest"
        )
    return normalized


def _request_sha256(value: dict[str, Any]) -> str:
    return legacy.sha256_json(value)


def _candidate_id_for_key(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"candidate-{digest[:24]}"


def _operator_context(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("operator_intake")
    return value if isinstance(value, dict) else {}


def _candidate_identity(event: dict[str, Any]) -> str:
    value = event["record"].get("candidate_id")
    return str(value or f"candidate-event-{event['event_id']}")


def _candidate_idempotency_result(
    store: StateStore, *, key: str, request_sha256: str
) -> dict[str, Any] | None:
    """Return the current idempotent replay or fail on a conflicting key."""
    for event in reversed(candidate_records(store)):
        context = _operator_context(event["record"])
        if context.get("idempotency_key") != key:
            continue
        if context.get("request_sha256") != request_sha256:
            raise OperatorIntakeError(
                "idempotency-conflict",
                "idempotency_key already identifies different candidate input",
                details={
                    "candidate_id": _candidate_identity(event),
                    "event_id": event["event_id"],
                    "existing_request_sha256": context.get("request_sha256"),
                    "requested_sha256": request_sha256,
                },
            )
        identity = _candidate_identity(event)
        try:
            observed = current_candidate_record(store, candidate_id=identity)
        except StateError:
            observed = event
        return {
            "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
            "kind": "bureau_candidate_record_result",
            "status": "existing",
            "effect_started": False,
            "retryable": False,
            "ambiguity": False,
            "required_readback": [],
            "idempotent_replay": True,
            "candidate_id": identity,
            "event_id": observed["event_id"],
            "created_at": observed["created_at"],
            "request_sha256": request_sha256,
            "record": observed["record"],
            "does_not_establish": observed["record"].get("does_not_establish", []),
        }
    return None


def candidate_record_request(
    registry: Registry | None,
    store: StateStore,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate the versioned JSON transport request before domain dispatch."""
    allowed = {
        "schema_version",
        "idempotency_key",
        "title",
        "source_kind",
        "desired_outcome",
        "repo",
        "source_locator",
        "source_sha256",
        "observed_at",
        "task_id",
        "candidate_id",
        "note",
        "catalog_validation",
    }
    if request.get("schema_version") != OPERATOR_INTAKE_SCHEMA_VERSION:
        raise OperatorIntakeError(
            "request-schema-unsupported",
            f"candidate request schema_version must be {OPERATOR_INTAKE_SCHEMA_VERSION}",
        )
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise OperatorIntakeError(
            "request-fields-unknown",
            "candidate request contains unknown fields",
            details={"unknown_fields": unknown},
        )
    payload = {key: value for key, value in request.items() if key != "schema_version"}
    return candidate_record(registry, store, **payload)


def candidate_record(
    registry: Registry | None,
    store: StateStore,
    *,
    idempotency_key: str,
    title: str,
    source_kind: str,
    desired_outcome: str,
    repo: str | None = None,
    source_locator: str | None = None,
    source_sha256: str | None = None,
    observed_at: str | None = None,
    task_id: str | None = None,
    candidate_id: str | None = None,
    note: str | None = None,
    catalog_validation: str = "strict",
) -> dict[str, Any]:
    """Record one source-bound candidate idempotently in the existing Live Register."""
    key = _checked_text(idempotency_key, field="idempotency_key", maximum=200, required=True)
    assert key is not None
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise OperatorIntakeError(
            "idempotency-key-invalid",
            "idempotency_key contains unsupported characters",
        )
    checked_title = _checked_text(title, field="title", maximum=240)
    checked_kind = _checked_text(source_kind, field="source_kind", maximum=80)
    checked_outcome = _checked_text(desired_outcome, field="desired_outcome", maximum=4000)
    checked_locator = _checked_text(
        source_locator, field="source_locator", maximum=2000, required=False
    )
    checked_sha = _checked_source_sha(source_sha256)
    checked_observed = _checked_text(observed_at, field="observed_at", maximum=80, required=False)
    checked_note = _checked_text(note, field="note", maximum=2000, required=False)
    request = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "idempotency_key": key,
        "title": checked_title,
        "source_kind": checked_kind,
        "desired_outcome": checked_outcome,
        "repo": repo,
        "source_locator": checked_locator,
        "source_sha256": checked_sha,
        "observed_at": checked_observed,
        "task_id": task_id,
        "candidate_id": candidate_id,
        "note": checked_note,
        "catalog_validation": catalog_validation,
    }
    request_sha = _request_sha256(request)
    replayed = _candidate_idempotency_result(store, key=key, request_sha256=request_sha)
    if replayed is not None:
        return replayed

    generated_observed_at = checked_observed or legacy.utc_now()
    selected_candidate_id = candidate_id or _candidate_id_for_key(key)
    context = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "idempotency_key": key,
        "request_sha256": request_sha,
        "source": {
            "kind": checked_kind,
            "locator": checked_locator,
            "sha256": checked_sha,
            "observed_at": generated_observed_at,
            "freshness": "digest-bound" if checked_sha else "unknown",
            "does_not_establish": [] if checked_sha else ["source_content_identity"],
        },
        "desired_outcome": checked_outcome,
        "does_not_establish": [
            "registry_task_truth",
            "queue_truth",
            "task_readiness",
            "claim_or_dispatch_authority",
        ],
    }
    try:
        recorded = live_register_record(
            registry,
            store,
            kind="candidate_task",
            title=str(checked_title),
            source="operator-intake",
            repo=repo,
            task_id=task_id,
            candidate_id=selected_candidate_id,
            status="observed",
            promotion_required=True,
            note=checked_note or str(checked_outcome),
            catalog_validation=catalog_validation,
            operator_context=context,
        )
    except OperatorIntakeError:
        raise
    except StateError as exc:
        replayed = _candidate_idempotency_result(store, key=key, request_sha256=request_sha)
        if replayed is not None:
            return replayed
        raise OperatorIntakeError(
            "candidate-record-invalid",
            str(exc),
            details={"catalog_validation": catalog_validation},
        ) from exc
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_candidate_record_result",
        "status": "recorded",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "idempotent_replay": False,
        "candidate_id": selected_candidate_id,
        "event_id": recorded["event_id"],
        "created_at": recorded["created_at"],
        "request_sha256": request_sha,
        "record": recorded["record"],
        "does_not_establish": recorded["nonclaims"],
    }


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value.casefold()))


def _similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _candidate_text(event: dict[str, Any]) -> str:
    record = event["record"]
    context = _operator_context(record)
    return " ".join(
        str(value)
        for value in (
            record.get("title"),
            record.get("note"),
            context.get("desired_outcome"),
        )
        if value
    )


def _task_text(task: Any) -> str:
    raw = task.raw
    return " ".join(str(value) for value in (task.title, raw.get("goal")) if value)


def candidate_assess(
    registry: Registry,
    store: StateStore,
    *,
    candidate_id: str | None = None,
    event_id: int | None = None,
    initiative: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Assess one current candidate without changing Registry or Live Register truth."""
    event = current_candidate_record(store, candidate_id=candidate_id, event_id=event_id)
    record = event["record"]
    identity = _candidate_identity(event)
    context = _operator_context(record)
    selected_initiative = initiative
    if selected_initiative is not None and selected_initiative not in registry.initiatives:
        raise OperatorIntakeError("initiative-unknown", f"unknown initiative {selected_initiative}")
    exact: list[dict[str, Any]] = []
    source = context.get("source") if isinstance(context.get("source"), dict) else {}
    source_sha = source.get("sha256")
    for existing in registry.tasks.values():
        metadata = existing.raw.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        binding = metadata.get("operator_intake")
        binding = binding if isinstance(binding, dict) else {}
        if binding.get("candidate_id") == identity:
            exact.append(
                {
                    "kind": "task-candidate-binding",
                    "task_id": existing.id,
                    "reason": "same candidate_id",
                }
            )
        existing_source = binding.get("source")
        if (
            source_sha
            and isinstance(existing_source, dict)
            and existing_source.get("sha256") == source_sha
        ):
            exact.append(
                {
                    "kind": "task-source-digest",
                    "task_id": existing.id,
                    "reason": "same source_sha256",
                }
            )
    if task_id and task_id in registry.tasks:
        exact.append({"kind": "task-id", "task_id": task_id, "reason": "task_id exists"})
    for other in current_candidate_records(store):
        if (
            int(other["event_id"]) == int(event["event_id"])
            or _candidate_identity(other) == identity
        ):
            continue
        other_context = _operator_context(other["record"])
        other_source = other_context.get("source")
        if (
            source_sha
            and isinstance(other_source, dict)
            and other_source.get("sha256") == source_sha
        ):
            exact.append(
                {
                    "kind": "candidate-source-digest",
                    "candidate_id": _candidate_identity(other),
                    "event_id": other["event_id"],
                    "reason": "same source_sha256",
                }
            )
    deduped_exact = list({legacy.canonical_json(item): item for item in exact}.values())
    candidate_text = _candidate_text(event)
    similar: list[dict[str, Any]] = []
    for existing in registry.tasks.values():
        score = _similarity(candidate_text, _task_text(existing))
        if score >= 0.2:
            similar.append(
                {
                    "kind": "task",
                    "id": existing.id,
                    "title": existing.title,
                    "score": round(score, 6),
                }
            )
    for other in current_candidate_records(store):
        if (
            int(other["event_id"]) == int(event["event_id"])
            or _candidate_identity(other) == identity
        ):
            continue
        score = _similarity(candidate_text, _candidate_text(other))
        if score >= 0.2:
            similar.append(
                {
                    "kind": "candidate",
                    "id": _candidate_identity(other),
                    "event_id": other["event_id"],
                    "title": other["record"].get("title"),
                    "score": round(score, 6),
                }
            )
    similar.sort(key=lambda item: (-float(item["score"]), item["kind"], item["id"]))
    missing: list[str] = []
    if not record.get("repo"):
        missing.append("repo")
    if not context.get("desired_outcome"):
        missing.append("desired_outcome")
    if not source.get("kind"):
        missing.append("source.kind")
    if not source.get("locator") and not source.get("sha256"):
        missing.append("source.locator_or_sha256")
    catalog = record.get("catalog_validation")
    deferred = isinstance(catalog, dict) and catalog.get("status") == "deferred"
    status = record.get("status")
    if status in {"closed", "dropped"}:
        decision = "drop"
    elif deduped_exact:
        decision = "merge"
    elif deferred:
        decision = "defer"
    elif missing:
        decision = "refine"
    else:
        decision = "promote"
    repo = record.get("repo")
    claims = [{"resource": repo, "mode": "write", "isolation": "worktree"}] if repo else []
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_candidate_assessment",
        "status": "assessed",
        "candidate_id": identity,
        "event_id": event["event_id"],
        "candidate_status": status,
        "decision": decision,
        "source_freshness": {
            "status": source.get("freshness", "unknown"),
            "observed_at": source.get("observed_at"),
            "sha256": source_sha,
            "catalog_validation": catalog,
        },
        "target": {
            "initiative": selected_initiative,
            "task_id": task_id,
            "claims": claims,
            "risk": "medium" if claims else "unknown",
            "implementation_approval": (
                task_approval_contract(
                    {
                        "id": task_id,
                        "execution": {
                            "mode": "interactive-agent",
                            "policy": "review-before-effect",
                        },
                        "claims": claims,
                    }
                )
                if task_id
                else None
            ),
            "publication_approval": approval_decision("registry_mutation", None),
        },
        "exact_duplicates": deduped_exact,
        "similarity_suggestions": similar[:MAX_SIMILARITY_RESULTS],
        "missing_fields": missing,
        "advisory_only": True,
        "does_not_establish": [
            "automatic_merge",
            "automatic_close",
            "automatic_suppression",
            "task_readiness",
            "registry_mutation",
        ],
    }


def _git_value(root: Path, *arguments: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()[:2000]
        raise OperatorIntakeError(
            "registry-git-read-failed",
            f"git {' '.join(arguments)} failed: {detail}",
            retryable=True,
        )
    return process.stdout.strip()


def _registry_identity(registry: Registry) -> dict[str, str]:
    return {
        "commit": _git_value(registry.root, "rev-parse", "HEAD"),
        "registry_tree": _git_value(registry.root, "rev-parse", "HEAD:registry"),
    }


def _render_task(task_json: dict[str, Any]) -> bytes:
    return (json.dumps(task_json, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _task_change_sha256(path: str, content: bytes) -> str:
    return legacy.sha256_json(
        {
            "path": path,
            "before": None,
            "after_sha256": hashlib.sha256(content).hexdigest(),
        }
    )


def _write_create_only(path: Path, content: bytes) -> None:
    target = path.expanduser().resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise OperatorIntakeError(
            "target-exists", f"refusing to overwrite existing file {target}"
        ) from exc
    except OSError as exc:
        raise OperatorIntakeError(
            "target-create-failed",
            f"cannot create output file {target}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(target)},
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise OperatorIntakeError(
            "target-write-failed",
            f"cannot durably write output file {target}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(target)},
        ) from exc


def _validate_task_semantics(registry: Registry, task_json: dict[str, Any]) -> None:
    try:
        registry.schemas.validate("task", task_json, "operator-intake-task")
    except Exception as exc:
        raise OperatorIntakeError(
            "task-schema-invalid", f"task JSON does not satisfy the task schema: {exc}"
        ) from exc
    task_id = str(task_json.get("id", ""))
    initiative = str(task_json.get("initiative", ""))
    if task_id in registry.tasks:
        raise OperatorIntakeError("task-exists", f"task {task_id} already exists")
    if initiative not in registry.initiatives:
        raise OperatorIntakeError("initiative-unknown", f"unknown initiative {initiative}")
    for dependency in task_json.get("depends_on", []):
        if dependency not in registry.tasks:
            raise OperatorIntakeError(
                "dependency-unknown", f"task dependency {dependency} is unknown"
            )
    for claim in task_json.get("claims", []):
        resource = claim.get("resource") if isinstance(claim, dict) else None
        if resource not in registry.resources:
            raise OperatorIntakeError(
                "claim-resource-unknown", f"claim resource {resource} is unknown"
            )
    if not task_json.get("claims"):
        raise OperatorIntakeError("claims-missing", "task proposal requires explicit claims")
    if not task_json.get("required_capabilities"):
        raise OperatorIntakeError(
            "capabilities-missing", "task proposal requires explicit capabilities"
        )
    if not task_json.get("acceptance"):
        raise OperatorIntakeError(
            "acceptance-missing", "task proposal requires explicit acceptance criteria"
        )


def _inject_candidate_binding(task_json: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(legacy.canonical_json(task_json))
    metadata = result.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise OperatorIntakeError("metadata-invalid", "task metadata must be an object")
    context = _operator_context(event["record"])
    metadata["operator_intake"] = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "candidate_id": _candidate_identity(event),
        "event_id": event["event_id"],
        "event_created_at": event["created_at"],
        "request_sha256": context.get("request_sha256"),
        "source": context.get("source"),
        "does_not_establish": [
            "queue_truth",
            "task_readiness",
            "claim_or_dispatch_authority",
        ],
    }
    return result


def task_propose(
    registry: Registry,
    store: StateStore,
    *,
    task_json: dict[str, Any],
    publishing_task_id: str,
    path: str | Path,
    candidate_id: str | None = None,
    event_id: int | None = None,
    unresolved_fields: Sequence[str] = (),
    placeholder_justification: str | None = None,
) -> dict[str, Any]:
    """Write one source-, Registry- and candidate-bound task proposal."""
    event = current_candidate_record(store, candidate_id=candidate_id, event_id=event_id)
    if event["record"].get("status") not in ACTIVE_LIVE_STATUSES:
        raise OperatorIntakeError(
            "candidate-not-open", "only a current open candidate can be proposed"
        )
    if publishing_task_id not in registry.tasks:
        raise OperatorIntakeError(
            "publishing-task-unknown",
            f"publishing task {publishing_task_id} is not in the Registry",
        )
    bound_task = _inject_candidate_binding(task_json, event)
    generic_ids = {
        criterion.get("id")
        for criterion in bound_task.get("acceptance", [])
        if isinstance(criterion, dict)
    } & _GENERIC_ACCEPTANCE_IDS
    if generic_ids and not _checked_text(
        placeholder_justification,
        field="placeholder_justification",
        maximum=2000,
        required=False,
    ):
        raise OperatorIntakeError(
            "generic-placeholder-rejected",
            "generic promotion acceptance requires explicit justification",
            details={"acceptance_ids": sorted(generic_ids)},
        )
    _validate_task_semantics(registry, bound_task)
    assessment = candidate_assess(
        registry,
        store,
        event_id=int(event["event_id"]),
        initiative=str(bound_task["initiative"]),
        task_id=str(bound_task["id"]),
    )
    if assessment["exact_duplicates"]:
        raise OperatorIntakeError(
            "exact-duplicate",
            "candidate assessment found an exact duplicate",
            details={"findings": assessment["exact_duplicates"]},
        )
    identity = _registry_identity(registry)
    task_id = str(bound_task["id"])
    target_path = f"registry/tasks/{task_id}.json"
    content = _render_task(bound_task)
    unresolved = sorted(
        {value.strip() for value in unresolved_fields if isinstance(value, str) and value.strip()}
    )
    proposal: dict[str, Any] = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_operator_task_proposal",
        "command": "operator-task-propose",
        "candidate": {
            "candidate_id": _candidate_identity(event),
            "event_id": event["event_id"],
            "event_created_at": event["created_at"],
            "event_sha256": legacy.sha256_json(event),
        },
        "registry": identity,
        "publishing_task_id": publishing_task_id,
        "task_id": task_id,
        "target_path": target_path,
        "task_json": bound_task,
        "task_json_sha256": legacy.sha256_json(bound_task),
        "task_file_sha256": hashlib.sha256(content).hexdigest(),
        "proposed_diff_sha256": _task_change_sha256(target_path, content),
        "assessment": assessment,
        "unresolved_fields": unresolved,
        "placeholder_justification": placeholder_justification,
        "publication": {
            "action_class": "registry_mutation",
            "required_level": "reviewed_plan",
            "queue_mutated": False,
        },
        "review": {
            "required": True,
            "status": "pending",
            "required_fields": [
                "reviewer",
                "reviewed_at",
                "reviewed_proposal_sha256",
            ],
        },
        "does_not_establish": [
            "registry_mutation",
            "queue_mutation",
            "task_readiness",
            "claim_or_dispatch_authority",
            "merge_or_deployment_authority",
        ],
    }
    unsigned = {
        key: value for key, value in proposal.items() if key not in {"proposal_sha256", "review"}
    }
    proposal["proposal_sha256"] = legacy.sha256_json(unsigned)
    rendered = (json.dumps(proposal, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    target = Path(path).expanduser()
    _write_create_only(target, rendered)
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_proposal_result",
        "status": "written",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "path": str(target),
        "proposal_sha256": proposal["proposal_sha256"],
        "plan_file_sha256": hashlib.sha256(rendered).hexdigest(),
        "proposal": proposal,
    }


def _proposal_unsigned(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in {"proposal_sha256", "review"}}


def _validated_proposal(
    registry: Registry,
    store: StateStore,
    *,
    plan_path: Path,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    try:
        plan_bytes = plan_path.read_bytes()
    except OSError as exc:
        raise OperatorIntakeError(
            "proposal-read-failed",
            f"cannot read task proposal {plan_path}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(plan_path)},
        ) from exc
    try:
        plan = json.loads(plan_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            "proposal-json-invalid", f"cannot parse task proposal: {exc}"
        ) from exc
    if not isinstance(plan, dict) or plan.get("kind") != "bureau_operator_task_proposal":
        raise OperatorIntakeError("proposal-kind-invalid", "unsupported operator task proposal")
    expected_proposal_sha = legacy.sha256_json(_proposal_unsigned(plan))
    if plan.get("proposal_sha256") != expected_proposal_sha:
        raise OperatorIntakeError(
            "proposal-integrity-invalid", "task proposal hash does not match its content"
        )
    review = plan.get("review")
    if not isinstance(review, dict):
        raise OperatorIntakeError("review-invalid", "proposal review must be an object")
    if review.get("status") != "reviewed":
        raise OperatorIntakeError("review-missing", "proposal review.status must be reviewed")
    reviewer = _checked_text(review.get("reviewer"), field="reviewer", maximum=200)
    _checked_text(review.get("reviewed_at"), field="reviewed_at", maximum=80)
    if review.get("reviewed_proposal_sha256") != expected_proposal_sha:
        raise OperatorIntakeError(
            "review-binding-invalid",
            "reviewed_proposal_sha256 does not match proposal_sha256",
        )
    approval = reviewed_plan_approval(
        reviewer=str(reviewer),
        reference=expected_proposal_sha,
        task_id=str(plan.get("task_id")),
        scope="registry_mutation",
    )
    approval_result = require_approval(
        "registry_mutation",
        approval,
        expected_reference=expected_proposal_sha,
        task_id=str(plan.get("task_id")),
    )
    identity = _registry_identity(registry)
    if plan.get("registry") != identity:
        raise OperatorIntakeError(
            "registry-drift",
            "Registry commit or tree changed after proposal creation",
            retryable=True,
            details={"planned": plan.get("registry"), "current": identity},
        )
    candidate = plan.get("candidate")
    if not isinstance(candidate, dict):
        raise OperatorIntakeError("candidate-binding-invalid", "proposal candidate is invalid")
    current = current_candidate_record(store, candidate_id=str(candidate.get("candidate_id")))
    if int(current["event_id"]) != int(candidate.get("event_id", -1)):
        raise OperatorIntakeError(
            "candidate-drift",
            "candidate was superseded after proposal creation",
            retryable=True,
        )
    task_json = plan.get("task_json")
    if not isinstance(task_json, dict):
        raise OperatorIntakeError("task-json-invalid", "proposal task_json is missing")
    _validate_task_semantics(registry, task_json)
    content = _render_task(task_json)
    target_path = str(plan.get("target_path"))
    expected_path = f"registry/tasks/{task_json.get('id')}.json"
    if target_path != expected_path:
        raise OperatorIntakeError("target-path-invalid", f"target path must be {expected_path}")
    if (registry.root / target_path).exists():
        raise OperatorIntakeError(
            "target-exists", f"target task file already exists: {target_path}"
        )
    if plan.get("task_json_sha256") != legacy.sha256_json(task_json):
        raise OperatorIntakeError("task-json-drift", "task_json_sha256 does not match task_json")
    if plan.get("task_file_sha256") != hashlib.sha256(content).hexdigest():
        raise OperatorIntakeError(
            "task-file-drift", "task_file_sha256 does not match rendered task JSON"
        )
    if plan.get("proposed_diff_sha256") != _task_change_sha256(target_path, content):
        raise OperatorIntakeError(
            "proposal-diff-drift", "proposed_diff_sha256 does not match task file change"
        )
    unresolved = plan.get("unresolved_fields")
    if not isinstance(unresolved, list) or unresolved:
        raise OperatorIntakeError(
            "proposal-unresolved",
            "reviewed proposal still contains unresolved fields",
            details={"unresolved_fields": unresolved},
        )
    return plan, plan_bytes, approval_result


def publication_preview(
    registry: Registry,
    store: StateStore,
    *,
    plan_path: str | Path,
) -> dict[str, Any]:
    path = Path(plan_path).expanduser().resolve()
    plan, plan_bytes, approval_result = _validated_proposal(registry, store, plan_path=path)
    task_id = str(plan["task_id"])
    required_keys = sorted(
        [
            f"path:{BUREAU_REPOSITORY_ROOT / plan['target_path']}",
            BUREAU_REGISTRY_PUBLICATION_GATE_KEY,
        ]
    )
    branch = _publication_branch(task_id, str(plan["proposal_sha256"]))
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_publication_preview",
        "status": "ready",
        "effect_started": False,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "plan_path": str(path),
        "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "proposal_sha256": plan["proposal_sha256"],
        "task_id": task_id,
        "target_path": plan["target_path"],
        "branch": branch,
        "required_resource_keys": required_keys,
        "approval": approval_result,
        "does_not_establish": [
            "lease_ownership",
            "branch_creation",
            "pull_request_creation",
            "queue_mutation",
            "merge_readiness",
        ],
    }


def _publication_branch(task_id: str, proposal_sha256: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.casefold()).strip("-")
    branch = f"operator/register-{slug}-{proposal_sha256[:10]}"
    if not _BRANCH_RE.fullmatch(branch):
        raise OperatorIntakeError("branch-invalid", "derived publication branch is invalid")
    return branch


def publish_task_proposal(
    registry: Registry,
    store: StateStore,
    *,
    plan_path: str | Path,
    lease_binding: dict[str, Any],
    workspace_root: str | Path,
    receipt_path: str | Path,
    resource_db: str | Path = DEFAULT_GRABOWSKI_RESOURCE_DB,
    publisher: TaskPublisher | None = None,
) -> dict[str, Any]:
    """Publish one reviewed task proposal without queue, merge or deployment effects."""
    path = Path(plan_path).expanduser().resolve()
    receipt = Path(receipt_path).expanduser().resolve()
    try:
        plan_bytes = path.read_bytes()
    except OSError as exc:
        raise OperatorIntakeError(
            "proposal-read-failed",
            f"cannot read task proposal {path}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(path)},
        ) from exc
    plan_file_sha = hashlib.sha256(plan_bytes).hexdigest()
    try:
        plan_for_replay = json.loads(plan_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            "proposal-json-invalid", f"cannot parse task proposal: {exc}"
        ) from exc
    if not isinstance(plan_for_replay, dict):
        raise OperatorIntakeError(
            "proposal-object-required", "task proposal JSON must be an object"
        )
    if receipt.exists():
        try:
            existing = json.loads(receipt.read_bytes())
        except OSError as exc:
            raise OperatorIntakeError(
                "receipt-read-failed",
                f"cannot read publication receipt {receipt}: {exc}",
                retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
                details={"path": str(receipt)},
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OperatorIntakeError(
                "receipt-invalid", f"cannot parse publication receipt: {exc}"
            ) from exc
        if not isinstance(existing, dict):
            raise OperatorIntakeError(
                "receipt-invalid", "publication receipt JSON must be an object"
            )
        unsigned_receipt = {
            key: value for key, value in existing.items() if key != "receipt_sha256"
        }
        receipt_valid = (
            existing.get("kind") == "bureau_task_publication_receipt"
            and existing.get("status") == "published"
            and existing.get("receipt_sha256") == legacy.sha256_json(unsigned_receipt)
            and existing.get("publication", {}).get("readback_complete") is True
        )
        if not receipt_valid:
            raise OperatorIntakeError(
                "receipt-integrity-invalid",
                "existing publication receipt is not a valid completed receipt",
            )
        if (
            existing.get("proposal_sha256") == plan_for_replay.get("proposal_sha256")
            and existing.get("plan_file_sha256") == plan_file_sha
        ):
            return {**existing, "idempotent_replay": True, "receipt_path": str(receipt)}
        raise OperatorIntakeError(
            "receipt-conflict", "existing publication receipt belongs to a different plan"
        )
    preview = publication_preview(registry, store, plan_path=path)
    plan = plan_for_replay
    if lease_binding.get("task_id") != plan["publishing_task_id"]:
        raise OperatorIntakeError(
            "lease-task-mismatch",
            "lease binding task_id must match the registered publishing task",
            details={
                "expected": plan["publishing_task_id"],
                "observed": lease_binding.get("task_id"),
            },
        )
    try:
        normalized_leases = validate_live_lease_binding(
            {"required_resource_keys": preview["required_resource_keys"]},
            lease_binding,
            resource_db=Path(resource_db),
            min_remaining_seconds=60,
            required_metadata={
                "task_id": plan["publishing_task_id"],
                "operation": "registry-publication",
                "proposal_sha256": plan["proposal_sha256"],
            },
        )
    except RuntimeRefreshError as exc:
        raise OperatorIntakeError(
            exc.code,
            str(exc),
            retryable=exc.code
            in {
                "lease-database-read-failed",
                "lease-expired",
                "lease-resources-missing",
            },
            details=exc.details,
        ) from exc
    gate_snapshot = next(
        item
        for item in normalized_leases["lease_snapshots"]
        if item["resource_key"] == BUREAU_REGISTRY_PUBLICATION_GATE_KEY
    )
    if gate_snapshot["expires_at_unix"] - gate_snapshot["acquired_at_unix"] > 300:
        raise OperatorIntakeError(
            "publication-gate-ttl-invalid",
            "registry publication gate lease must be bounded to at most 300 seconds",
        )

    def assert_plan_unchanged() -> None:
        try:
            current_bytes = path.read_bytes()
        except OSError as exc:
            raise OperatorIntakeError(
                "plan-read-failed",
                f"cannot reread reviewed plan {path}: {exc}",
                retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
                details={"path": str(path)},
            ) from exc
        observed = hashlib.sha256(current_bytes).hexdigest()
        if observed != plan_file_sha:
            raise OperatorIntakeError(
                "plan-file-drift",
                "reviewed plan bytes changed before publication effect",
                retryable=True,
            )

    assert_plan_unchanged()
    selected_publisher = publisher or SubprocessTaskPublisher()
    try:
        published = selected_publisher.publish(
            registry=registry,
            plan=plan,
            workspace_root=Path(workspace_root).expanduser().resolve(),
            assert_plan_unchanged=assert_plan_unchanged,
        )
    except OperatorIntakeError:
        raise
    except Exception as exc:
        raise OperatorIntakeError(
            "publication-unclear",
            f"publisher failed with unclear outcome: {exc}",
            retryable=False,
            effect_started=True,
            ambiguity=True,
            required_readback=[
                "remote branch head",
                "open pull request for exact branch",
                "target task file at remote head",
            ],
        ) from exc
    assert_plan_unchanged()
    value: dict[str, Any] = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_publication_receipt",
        "status": "published",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "proposal_sha256": preview["proposal_sha256"],
        "plan_file_sha256": plan_file_sha,
        "task_id": preview["task_id"],
        "target_path": preview["target_path"],
        "branch": preview["branch"],
        "registry": plan["registry"],
        "publishing_task_id": plan["publishing_task_id"],
        "lease_binding": normalized_leases,
        "publication": published,
        "created_at": legacy.utc_now(),
        "queue_mutated": False,
        "does_not_establish": [
            "task_readiness",
            "claim_or_dispatch_authority",
            "merge_or_deployment_authority",
            "task_verification",
        ],
    }
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    value["receipt_sha256"] = legacy.sha256_json(unsigned)
    receipt_bytes = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        _write_create_only(receipt, receipt_bytes)
    except (OSError, OperatorIntakeError) as exc:
        raise OperatorIntakeError(
            "receipt-write-unclear",
            f"publication succeeded but receipt write failed: {exc}",
            retryable=False,
            effect_started=True,
            ambiguity=True,
            required_readback=[
                "remote branch head",
                "open pull request for exact branch",
                f"publication receipt at {receipt}",
            ],
            details={
                "proposal_sha256": preview["proposal_sha256"],
                "branch": preview["branch"],
                "publication": published,
            },
        ) from exc
    return {**value, "idempotent_replay": False, "receipt_path": str(receipt)}


class SubprocessTaskPublisher:
    """Narrow Git/GitHub transport for one already reviewed task-file publication."""

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int = 60,
    ) -> str:
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        process = subprocess.run(
            list(arguments),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
        if process.returncode != 0:
            detail = "\n".join(
                part for part in (process.stdout.strip(), process.stderr.strip()) if part
            )[:4000]
            raise OperatorIntakeError(
                "publication-command-failed",
                f"{' '.join(arguments)} failed: {detail}",
                effect_started=arguments[:2] in (["git", "push"], ["gh", "pr"]),
                ambiguity=arguments[:2] in (["git", "push"], ["gh", "pr"]),
                required_readback=(
                    ["remote branch head", "open pull request"]
                    if arguments[:2] in (["git", "push"], ["gh", "pr"])
                    else []
                ),
            )
        return process.stdout.strip()

    @staticmethod
    def _github_slug(remote: str) -> str:
        value = remote.strip()
        if value.startswith("git@github.com:"):
            value = value.removeprefix("git@github.com:")
        elif "github.com/" in value:
            value = value.split("github.com/", 1)[1]
        value = value.removesuffix(".git").strip("/")
        if value.count("/") != 1:
            raise OperatorIntakeError(
                "github-remote-invalid", "origin remote is not a GitHub repository"
            )
        return value

    def publish(
        self,
        *,
        registry: Registry,
        plan: dict[str, Any],
        workspace_root: Path,
        assert_plan_unchanged: Callable[[], None],
    ) -> dict[str, Any]:
        base_commit = str(plan["registry"]["commit"])
        task_id = str(plan["task_id"])
        target_path = str(plan["target_path"])
        branch = _publication_branch(task_id, str(plan["proposal_sha256"]))
        remote = self._run(["git", "-C", str(registry.root), "remote", "get-url", "origin"])
        repository = self._github_slug(remote)
        remote_main_output = self._run(["git", "ls-remote", remote, "refs/heads/main"]).split()
        if not remote_main_output:
            raise OperatorIntakeError(
                "remote-main-missing", "remote main ref is missing", retryable=True
            )
        remote_main = remote_main_output[0]
        if remote_main != base_commit:
            raise OperatorIntakeError(
                "remote-main-drift",
                "remote main changed after proposal creation",
                retryable=True,
                details={"planned": base_commit, "observed": remote_main},
            )
        existing_branch = self._run(["git", "ls-remote", remote, f"refs/heads/{branch}"])
        if existing_branch:
            raise OperatorIntakeError(
                "remote-branch-exists",
                f"publication branch already exists: {branch}",
                required_readback=["remote branch head", "open pull request"],
            )
        workspace = workspace_root / str(plan["proposal_sha256"])[:20]
        if workspace.exists():
            raise OperatorIntakeError(
                "workspace-exists", f"publication workspace already exists: {workspace}"
            )
        workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._run(
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--no-local",
                "--no-checkout",
                str(registry.root),
                str(workspace),
            ]
        )
        self._run(["git", "remote", "set-url", "origin", remote], cwd=workspace)
        self._run(["git", "checkout", "--detach", base_commit], cwd=workspace)
        self._run(["git", "checkout", "-b", branch], cwd=workspace)
        task_file = workspace / target_path
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_bytes(_render_task(plan["task_json"]))
        Registry.load(workspace)
        changed = self._run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace
        ).splitlines()
        if changed != [f"?? {target_path}"]:
            raise OperatorIntakeError(
                "publication-scope-drift",
                "publication workspace changed outside the target task file",
                details={"status": changed},
            )
        self._run(["git", "add", "--", target_path], cwd=workspace)
        staged = self._run(["git", "diff", "--cached", "--name-only"], cwd=workspace).splitlines()
        if staged != [target_path]:
            raise OperatorIntakeError(
                "publication-scope-drift",
                "staged publication diff is not exactly the target task file",
                details={"paths": staged},
            )
        diff = self._run(["git", "diff", "--cached", "--binary"], cwd=workspace)
        assert_plan_unchanged()
        self._run(
            [
                "git",
                "-c",
                "user.name=Bureau Operator",
                "-c",
                "user.email=bureau-operator@localhost",
                "commit",
                "-m",
                f"Register Bureau task {task_id}",
            ],
            cwd=workspace,
        )
        head = self._run(["git", "rev-parse", "HEAD"], cwd=workspace)
        remote_main_output = self._run(["git", "ls-remote", remote, "refs/heads/main"]).split()
        if not remote_main_output:
            raise OperatorIntakeError(
                "remote-main-missing", "remote main ref is missing", retryable=True
            )
        remote_main = remote_main_output[0]
        if remote_main != base_commit:
            raise OperatorIntakeError(
                "remote-main-drift",
                "remote main changed immediately before publication push",
                retryable=True,
            )
        assert_plan_unchanged()
        self._run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=workspace)
        body = (
            f"Bureau-Task: {plan['publishing_task_id']}\n\n"
            f"Register reviewed candidate task `{task_id}`.\n\n"
            f"- proposal: `{plan['proposal_sha256']}`\n"
            f"- candidate: `{plan['candidate']['candidate_id']}`\n"
            f"- source event: `{plan['candidate']['event_id']}`\n"
            f"- target: `{target_path}`\n\n"
            "This PR does not queue, claim, dispatch, merge, deploy or verify the task.\n"
        )
        url = self._run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repository,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                f"Register Bureau task {task_id}",
                "--body",
                body,
            ],
            cwd=workspace,
        )
        readback_raw = self._run(
            [
                "gh",
                "pr",
                "view",
                url,
                "--repo",
                repository,
                "--json",
                "number,url,state,headRefOid,headRefName,baseRefName",
            ],
            cwd=workspace,
        )
        readback = json.loads(readback_raw)
        if (
            readback.get("headRefOid") != head
            or readback.get("headRefName") != branch
            or readback.get("baseRefName") != "main"
            or readback.get("state") != "OPEN"
        ):
            raise OperatorIntakeError(
                "publication-readback-mismatch",
                "GitHub pull-request readback does not match the published branch",
                effect_started=True,
                ambiguity=True,
                required_readback=["pull request metadata", "remote branch head"],
                details={"readback": readback, "expected_head": head},
            )
        return {
            "repository": repository,
            "workspace": str(workspace),
            "branch": branch,
            "head": head,
            "pull_request": readback,
            "url": readback.get("url") or url,
            "git_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "target_file_sha256": hashlib.sha256(task_file.read_bytes()).hexdigest(),
            "readback_complete": True,
        }
