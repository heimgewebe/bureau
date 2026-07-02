from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft7Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .legacy import ValidationError, atomic_write, canonical_json, read_json, sha256_json
from .schema_validation import DocumentSchemaError, SchemaSet

SOURCE_NAME = "weltgewebe"
SOURCE_SYSTEM = "weltgewebe-task-control"
SOURCE_REPOSITORY = "heimgewebe/weltgewebe"
INDEX_PATH = "docs/tasks/index.json"
SCHEMA_PATH = "docs/tasks/schema.json"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
PREVIEW_LIMIT = 50
SOURCE_STATUSES = ("open", "partial", "done", "blocked", "obsolete", "contradicted")
ACTIVE_STATUSES = {"open", "partial", "blocked"}
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SOURCE_ID_RE = re.compile(r"^[A-Z]+(?:-[A-Z]+)*-[0-9]{3}$")


@dataclass(frozen=True)
class SourceSnapshot:
    repository: Path
    ref: str
    commit_sha: str
    index_sha256: str
    schema_sha256: str
    document: dict[str, Any]


def _safe_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _REF_RE.fullmatch(value)
        or value.startswith("-")
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", "."))
        or "/." in value
    ):
        raise ValidationError(f"invalid Git ref {value!r}")
    return value


def _safe_repo_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or any(part in {"", "."} for part in path.parts)
    ):
        raise ValidationError(f"invalid repository-relative path {value!r}")
    return value


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _git_read(repository: Path, arguments: list[str], *, binary: bool = False) -> bytes | str:
    command = [
        "git",
        "--no-pager",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.pager=cat",
        "-c",
        "diff.external=",
        "-c",
        "interactive.diffFilter=",
        "-C",
        str(repository),
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=not binary,
            timeout=30,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"Git source read failed: {exc}") from exc
    stdout = result.stdout
    stderr = result.stderr
    if result.returncode:
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = str(stderr).strip().splitlines()
        raise ValidationError(
            f"Git source read failed: {detail[-1] if detail else f'exit {result.returncode}'}"
        )
    size = len(stdout) if isinstance(stdout, bytes) else len(stdout.encode("utf-8"))
    if size > MAX_SOURCE_BYTES:
        raise ValidationError("Git source document exceeds the bounded read limit")
    return stdout


def _resolve_commit(repository: Path, ref: str) -> str:
    resolved = str(
        _git_read(
            repository,
            ["rev-parse", "--verify", "--end-of-options", f"{_safe_ref(ref)}^{{commit}}"],
        )
    ).strip()
    if not _COMMIT_RE.fullmatch(resolved):
        raise ValidationError(f"source ref did not resolve to one commit: {ref}")
    return resolved


def _read_commit_file(repository: Path, commit: str, relative_path: str) -> bytes:
    if not _COMMIT_RE.fullmatch(commit):
        raise ValidationError(f"invalid source commit {commit!r}")
    value = _git_read(
        repository,
        ["cat-file", "blob", f"{commit}:{_safe_repo_path(relative_path)}"],
        binary=True,
    )
    assert isinstance(value, bytes)
    return value


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot parse {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must contain one JSON object")
    return value


def _reject_external_refs(value: Any) -> None:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            raise ValidationError("Weltgewebe source schema may not resolve external references")
        for child in value.values():
            _reject_external_refs(child)
    elif isinstance(value, list):
        for child in value:
            _reject_external_refs(child)


def _validate_index(document: dict[str, Any], schema: dict[str, Any]) -> None:
    if schema.get("$schema") != "http://json-schema.org/draft-07/schema#":
        raise ValidationError("Weltgewebe source schema must declare JSON Schema Draft-07")
    _reject_external_refs(schema)
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValidationError(f"invalid Weltgewebe source schema: {exc.message}") from exc
    errors = sorted(
        Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            rendered.append(f"{location}: {error.message}")
        raise ValidationError("invalid Weltgewebe task index:\n" + "\n".join(rendered))
    identifiers = [str(task["id"]) for task in document.get("tasks", [])]
    duplicates = sorted(
        identifier for identifier, count in Counter(identifiers).items() if count > 1
    )
    if duplicates:
        raise ValidationError(f"duplicate Weltgewebe task ids: {', '.join(duplicates)}")


def load_snapshot(repository: str | Path, ref: str = "origin/main") -> SourceSnapshot:
    repo = Path(repository).expanduser().resolve()
    if not repo.is_dir():
        raise ValidationError(f"Weltgewebe repository does not exist: {repo}")
    commit = _resolve_commit(repo, ref)
    index_raw = _read_commit_file(repo, commit, INDEX_PATH)
    schema_raw = _read_commit_file(repo, commit, SCHEMA_PATH)
    document = _json_object(index_raw, f"{commit}:{INDEX_PATH}")
    schema = _json_object(schema_raw, f"{commit}:{SCHEMA_PATH}")
    _validate_index(document, schema)
    return SourceSnapshot(
        repository=repo,
        ref=ref,
        commit_sha=commit,
        index_sha256=hashlib.sha256(index_raw).hexdigest(),
        schema_sha256=hashlib.sha256(schema_raw).hexdigest(),
        document=document,
    )


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    links = task["links"]
    return {
        "id": task["id"],
        "title": task["title"],
        "area": task["area"],
        "status": task["status"],
        "priority": task["priority"],
        "effort": task["effort"],
        "risk": task["risk"],
        "owner": task["owner"],
        "updated_at": task["updated_at"],
        "source_task_sha256": sha256_json(task),
        "source_task": task,
        "evidence_count": len(task["evidence"]),
        "missing_evidence_count": len(task["missing_evidence"]),
        "acceptance_count": len(task["acceptance"]),
        "link_counts": {
            "issues": len(links["issues"]),
            "prs": len(links["prs"]),
            "docs": len(links["docs"]),
        },
    }


def build_source_document(snapshot: SourceSnapshot) -> dict[str, Any]:
    entries = sorted(
        (_task_summary(task) for task in snapshot.document["tasks"]),
        key=lambda item: item["id"],
    )
    status_counts = {status: 0 for status in SOURCE_STATUSES}
    for entry in entries:
        status_counts[entry["status"]] += 1
    return {
        "schema_version": 1,
        "source": SOURCE_NAME,
        "source_system": SOURCE_SYSTEM,
        "repository": SOURCE_REPOSITORY,
        "ref": snapshot.ref,
        "commit_sha": snapshot.commit_sha,
        "index_path": INDEX_PATH,
        "schema_path": SCHEMA_PATH,
        "index_sha256": snapshot.index_sha256,
        "schema_sha256": snapshot.schema_sha256,
        "source_schema_version": snapshot.document["schema_version"],
        "curation": snapshot.document["curation"],
        "source_files": list(snapshot.document["source_files"]),
        "task_count": len(entries),
        "status_counts": status_counts,
        "active_task_ids": [entry["id"] for entry in entries if entry["status"] in ACTIVE_STATUSES],
        "entries": entries,
        "does_not_establish": [
            "bureau_task_materialization",
            "bureau_task_readiness",
            "dependency_completeness",
            "safe_parallel_write_scope",
            "autonomous_execution_permission",
            "source_claim_truth",
        ],
    }


def validate_source_document(value: dict[str, Any]) -> None:
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise ValidationError("source snapshot entries must be an array")
    identifiers = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    if len(identifiers) != len(entries) or identifiers != sorted(identifiers):
        raise ValidationError("source snapshot entries must be uniquely sorted by id")
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError("source snapshot entries contain duplicate ids")
    if value.get("task_count") != len(entries):
        raise ValidationError("source snapshot task_count does not match entries")
    expected_counts = {status: 0 for status in SOURCE_STATUSES}
    expected_active: list[str] = []
    for entry in entries:
        source_task = entry.get("source_task")
        if not isinstance(source_task, dict):
            raise ValidationError("source snapshot entry is missing source_task")
        identifier = entry["id"]
        status = entry.get("status")
        if source_task.get("id") != identifier or source_task.get("status") != status:
            raise ValidationError(f"source snapshot entry identity mismatch: {identifier}")
        if entry.get("source_task_sha256") != sha256_json(source_task):
            raise ValidationError(f"source snapshot task hash mismatch: {identifier}")
        for field in ("title", "area", "priority", "effort", "risk", "owner", "updated_at"):
            if entry.get(field) != source_task.get(field):
                raise ValidationError(f"source snapshot summary mismatch for {identifier}: {field}")
        links = source_task.get("links", {})
        expected_link_counts = {
            "issues": len(links.get("issues", [])),
            "prs": len(links.get("prs", [])),
            "docs": len(links.get("docs", [])),
        }
        expected_entry_counts = {
            "evidence_count": len(source_task.get("evidence", [])),
            "missing_evidence_count": len(source_task.get("missing_evidence", [])),
            "acceptance_count": len(source_task.get("acceptance", [])),
        }
        if entry.get("link_counts") != expected_link_counts or any(
            entry.get(name) != count for name, count in expected_entry_counts.items()
        ):
            raise ValidationError(f"source snapshot evidence counts mismatch: {identifier}")
        expected_counts[status] += 1
        if status in ACTIVE_STATUSES:
            expected_active.append(identifier)
    if value.get("status_counts") != expected_counts:
        raise ValidationError("source snapshot status_counts do not match entries")
    if value.get("active_task_ids") != expected_active:
        raise ValidationError("source snapshot active_task_ids do not match entries")


def bureau_task_id(source_task_id: str) -> str:
    if not _SOURCE_ID_RE.fullmatch(source_task_id):
        raise ValidationError(f"unsupported Weltgewebe source task id {source_task_id!r}")
    return f"WG-{source_task_id}"


def _projected_state(source_status: str) -> str:
    if source_status in {"open", "partial"}:
        return "planned"
    if source_status == "blocked":
        return "blocked"
    if source_status in {"done", "obsolete", "contradicted"}:
        return "superseded"
    raise ValidationError(f"unsupported Weltgewebe source status {source_status!r}")


def _load_materialized_source(root: Path, source: str) -> dict[str, Any]:
    if source != SOURCE_NAME:
        raise ValidationError(f"unsupported source {source!r}")
    target = root.resolve() / "registry" / "sources" / "weltgewebe.json"
    if not target.is_file():
        raise ValidationError("Weltgewebe source snapshot has not been materialized")
    snapshot = read_json(target)
    try:
        SchemaSet(root.resolve() / "schemas").validate("source", snapshot, target)
    except DocumentSchemaError as exc:
        raise ValidationError(str(exc)) from exc
    validate_source_document(snapshot)
    return snapshot


def _source_entry(snapshot: dict[str, Any], source_task_id: str) -> dict[str, Any]:
    for entry in snapshot["entries"]:
        if entry["id"] == source_task_id:
            return entry
    raise ValidationError(f"unknown Weltgewebe source task id {source_task_id!r}")


def source_promote_plan(root: Path, registry: Any, source: str, task_id: str) -> dict[str, Any]:
    snapshot = _load_materialized_source(root, source)
    entry = _source_entry(snapshot, task_id)
    source_task = entry["source_task"]
    target_id = bureau_task_id(entry["id"])
    existing = registry.tasks.get(target_id)
    projected_state = _projected_state(entry["status"])
    blockers: list[str] = []
    if entry["status"] not in ACTIVE_STATUSES:
        blockers.append("source-task-is-not-active")
    if existing is not None:
        blockers.append("bureau-task-already-exists")
    if not source_task.get("acceptance"):
        blockers.append("source-task-has-no-acceptance")
    manual_decisions = [
        {
            "field": "initiative",
            "reason": "WG-WELTGEWEBE is only a candidate namespace until explicitly registered",
        },
        {
            "field": "claims",
            "reason": "the source index does not define safe write scope or resource isolation",
        },
        {"field": "dependencies", "reason": "the source index has no structured dependency graph"},
        {
            "field": "execution.policy",
            "reason": "source priority does not imply autonomous execution permission",
        },
    ]
    candidate_task = {
        "schema_version": 1,
        "id": target_id,
        "initiative": "WG-WELTGEWEBE",
        "title": source_task["title"],
        "state": projected_state,
        "goal": source_task["title"],
        "depends_on": [],
        "required_capabilities": ["repository", "shell"],
        "priority": {
            "lane": "later",
            "rank": {"high": 20, "medium": 50, "low": 80}[source_task["priority"]],
        },
        "execution": {
            "mode": "interactive-agent",
            "policy": "review-before-effect",
            "working_repository": str(Path.home() / "repos/weltgewebe"),
            "baseline_commit": snapshot["commit_sha"],
        },
        "claims": [{"resource": "repo.weltgewebe", "mode": "write", "isolation": "worktree"}],
        "acceptance": [
            {"id": f"source-{index:02d}", "assertion": assertion}
            for index, assertion in enumerate(source_task["acceptance"], 1)
        ],
        "metadata": {
            "source": {
                "system": SOURCE_SYSTEM,
                "repository": snapshot["repository"],
                "ref": snapshot["ref"],
                "commit_sha": snapshot["commit_sha"],
                "index_path": snapshot["index_path"],
                "schema_path": snapshot["schema_path"],
                "index_sha256": snapshot["index_sha256"],
                "schema_sha256": snapshot["schema_sha256"],
                "source_task_id": entry["id"],
                "source_task_sha256": entry["source_task_sha256"],
                "source_status": entry["status"],
                "source_priority": entry["priority"],
                "source_risk": entry["risk"],
                "source_effort": entry["effort"],
                "source_owner": entry["owner"],
                "source_updated_at": entry["updated_at"],
            }
        },
    }
    return {
        "valid": True,
        "source": SOURCE_NAME,
        "source_task_id": entry["id"],
        "bureau_task_id": target_id,
        "source_binding": {
            "repository": snapshot["repository"],
            "ref": snapshot["ref"],
            "commit_sha": snapshot["commit_sha"],
            "index_sha256": snapshot["index_sha256"],
            "schema_sha256": snapshot["schema_sha256"],
            "source_task_sha256": entry["source_task_sha256"],
        },
        "source_status": entry["status"],
        "projected_state": projected_state,
        "existing_bureau_task": None
        if existing is None
        else {"id": existing.id, "state": existing.state, "sha256": existing.sha256},
        "materialization_allowed": not blockers,
        "readiness": "blocked" if blockers or manual_decisions else "candidate",
        "blockers": blockers,
        "manual_decisions_required": manual_decisions,
        "candidate_task": candidate_task,
        "does_not_establish": [
            "bureau_task_materialization",
            "bureau_task_readiness",
            "dependency_completeness",
            "safe_parallel_write_scope",
            "autonomous_execution_permission",
            "source_claim_truth",
        ],
    }


def _bounded(values: list[str]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "ids": ordered[:PREVIEW_LIMIT],
        "truncated": len(ordered) > PREVIEW_LIMIT,
    }


def _change_summary(existing: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    old_entries = {
        str(entry["id"]): str(entry["source_task_sha256"])
        for entry in (existing or {}).get("entries", [])
        if isinstance(entry, dict) and "id" in entry and "source_task_sha256" in entry
    }
    new_entries = {
        str(entry["id"]): str(entry["source_task_sha256"]) for entry in candidate["entries"]
    }
    added = sorted(set(new_entries) - set(old_entries))
    removed = sorted(set(old_entries) - set(new_entries))
    changed = sorted(
        identifier
        for identifier in set(old_entries) & set(new_entries)
        if old_entries[identifier] != new_entries[identifier]
    )
    return {
        "added": _bounded(added),
        "changed": _bounded(changed),
        "removed": _bounded(removed),
    }


def _public_report(candidate: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    active_ids = list(candidate["active_task_ids"])
    return {
        "valid": True,
        "source": candidate["source"],
        "repository": candidate["repository"],
        "ref": candidate["ref"],
        "commit_sha": candidate["commit_sha"],
        "index_sha256": candidate["index_sha256"],
        "schema_sha256": candidate["schema_sha256"],
        "task_count": candidate["task_count"],
        "status_counts": candidate["status_counts"],
        "active_tasks": _bounded(active_ids),
        "changes": changes,
        "does_not_establish": candidate["does_not_establish"],
    }


def source_check(repository: str | Path, ref: str = "origin/main") -> dict[str, Any]:
    candidate = build_source_document(load_snapshot(repository, ref))
    no_drift = {name: _bounded([]) for name in ("added", "changed", "removed")}
    return _public_report(candidate, no_drift)


_PROVENANCE_ONLY_FIELDS = frozenset({"ref", "commit_sha"})


def _source_content(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: item for key, item in value.items() if key not in _PROVENANCE_ONLY_FIELDS}


def source_sync(
    root: Path,
    repository: str | Path,
    ref: str = "origin/main",
    *,
    apply: bool = False,
) -> dict[str, Any]:
    candidate = build_source_document(load_snapshot(repository, ref))
    validate_source_document(candidate)
    target = root.resolve() / "registry" / "sources" / "weltgewebe.json"
    existing = read_json(target) if target.exists() else None
    changes = _change_summary(existing, candidate)
    rendered = json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    changed = _source_content(existing) != _source_content(candidate)
    applied = False
    if apply and changed:
        try:
            SchemaSet(root.resolve() / "schemas").validate("source", candidate, target)
        except DocumentSchemaError as exc:
            raise ValidationError(str(exc)) from exc
        atomic_write(target, rendered)
        applied = True
    report = _public_report(candidate, changes)
    report.update(
        {
            "target": str(target),
            "changed": changed,
            "applied": applied,
            "document_sha256": hashlib.sha256(
                canonical_json(candidate).encode("utf-8")
            ).hexdigest(),
        }
    )
    return report
