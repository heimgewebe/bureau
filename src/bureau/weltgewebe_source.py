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
    Draft7Validator.check_schema(schema)
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
        str(entry["id"]): str(entry["source_task_sha256"])
        for entry in candidate["entries"]
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


def source_sync(
    root: Path,
    repository: str | Path,
    ref: str = "origin/main",
    *,
    apply: bool = False,
) -> dict[str, Any]:
    candidate = build_source_document(load_snapshot(repository, ref))
    target = root.resolve() / "registry" / "sources" / "weltgewebe.json"
    existing = read_json(target) if target.exists() else None
    changes = _change_summary(existing, candidate)
    rendered = json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    current = target.read_text(encoding="utf-8") if target.exists() else None
    changed = current != rendered
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
