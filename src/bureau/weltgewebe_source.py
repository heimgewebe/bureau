from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import FormatChecker, validators
from jsonschema.exceptions import SchemaError

from .core import Registry, ValidationError, atomic_write, canonical_json

SOURCE_NAME = "weltgewebe"
SOURCE_SYSTEM = "weltgewebe-task-control"
INDEX_PATH = "docs/tasks/index.json"
SCHEMA_PATH = "docs/tasks/schema.json"
SNAPSHOT_PATH = Path("registry/sources/weltgewebe.json")
_PREVIEW_LIMIT = 20
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_DOES_NOT_ESTABLISH = [
    "bureau_task_readiness",
    "dependency_completeness",
    "safe_parallel_write_scope",
    "autonomous_execution_permission",
    "source_claim_truth",
]


def _checked_ref(value: str) -> str:
    if (
        not _REF_RE.fullmatch(value)
        or ".." in value
        or "@{" in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValidationError(f"invalid Git ref {value!r}")
    return value


def _checked_repo_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or any(character.isspace() for character in value)
    ):
        raise ValidationError(f"invalid repository-relative source path {value!r}")
    return value


def _git(repository: Path, arguments: list[str], *, binary: bool = False) -> str | bytes:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
    }
    command = [
        "git",
        "--no-pager",
        "-c",
        "core.pager=cat",
        "-c",
        "pager.show=false",
        "-c",
        "pager.rev-parse=false",
        "-c",
        "diff.external=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-C",
        str(repository),
        *arguments,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=not binary,
        timeout=30,
        env=environment,
    )
    if completed.returncode:
        error = completed.stderr
        if isinstance(error, bytes):
            error = error.decode("utf-8", errors="replace")
        raise ValidationError(
            f"Git source read failed ({completed.returncode}): {str(error).strip()}"
        )
    return completed.stdout


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot parse {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must contain one JSON object")
    return value


def _validate(document: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    validator_class = validators.validator_for(schema)
    try:
        validator_class.check_schema(schema)
    except SchemaError as exc:
        raise ValidationError(f"invalid schema for {label}: {exc.message}") from exc
    errors = sorted(
        validator_class(schema, format_checker=Format
        Checker()).iter_errors(document),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    if not errors:
        return
    details = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        details.append(f"{location}: {error.message}")
    raise ValidationError(f"invalid {label}:\n" + "\n".join(details))


def _resolve_commit(repository: Path, ref: str) -> str:
    value = _git(
        repository,
        ["rev-parse", "--verify", "--end-of-options", f"{_checked_ref(ref)}^{ commit }"],
    )
    commit = str(value).strip()
    if not _COMMIT_RE.fullmatch(commit):
        raise ValidationError(f"source ref did not resolve to a commit: {ref}")
    return commit


def _read_commit_file(repository: Path, commit: str, path: str) -> bytes:
    value = _git(
        repository,
        ["show", "--no-ext-diff", f"{commit}:{_checked_repo_path(path)}"],
        binary=True,
    )
    assert isinstance(value, bytes)
    return value


def _source_task_id(task: dict[str, Any], position: int) -> str:
    value = task.get("id")
    if not isinstance(value, str) or not value:
        raise ValidationError(f"source task at index {position} has no non-empty string id")
    return value


def build_snapshot(
    repository: str | Path,
    ref: str = "origin/main",
    *,
    index_path: str = INDEX_PATH,

    schema_path: str = SCHEMA_PATH,
) -> dict[str, Any]:
    repo = Path(repository).expanduser().resolve()
    if not repo.is_dir():
        raise ValidationError(f"Weltgewebe repository does not exist: {repo}")
    commit = _resolve_commit(repo, ref)
    safe_index = _checked_repo_path(index_path)
    safe_schema = _checked_repo_path(schema_path)
    index_raw = _read_commit_file(repo, commit, safe_index)
    schema_raw = _read_commit_file(repo, commit, safe_schema)
    index = _json_object(index_raw, f"{commit}:{safe_index}")
    schema = _json_object(schema_raw, f"{commit}:{safe_schema}")
    _validate(index, schema, "Weltgewebe task index")
    raw_tasks = index.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValidationError("validated Weltgewebe task index has no tasks array")
    tasks = []
    seen: set[str] = set()
    statuses: Counter[str] = Counter()
    for position, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            raise ValidationError(f"source task at index {position} is not an object")
        task_id = _source_task_id(raw_task, position)
        if task_id in seen:
            raise ValidationError(f"duplicate source task id {task_id}")
        seen.add(task_id)
        status = str(raw_task.get("status", "unknown"))
        statuses[status] += 1
        tasks.append(
            {
                "source_task_id": task_id,
                "source_task_sha256": hashlib.sha256(
                    canonical_json(raw_task).encode("utf-8")
                ).hexdigest(),
                "task": raw_task,
            }
        )
    return {
        "schema_version": 1,
        "kind": "bureau.source_snapshot",
        "source": {
            "name": SOURCE_NAME,
            "system": SOURCE_SYSTEM,
            "repository": str(repo),
            "ref": _checked_ref(ref),
            "commit_sha": commit,
        },
        "index": {
            "path": safe_index,
            "sha256": hashlib.sha256(index_raw).hexdigest(),
        },
        "schema": {
            "path": safe_schema,
            "sha256": hashlib.sha256(schema_raw).hexdigest(),
        },
        "task_count": len(tasks),
        "status_counts": dict(sorted(statuses.items())),
        "tasks": tasks,
        "does_not_establish": list(_DOES_NOT_ESTABLISH),
    }


def _snapshot_schema(root: Path) -> dict[str, Any]:
    path = root / "schemas/source-snapshot.v1.schema.json"
    if not path.is_file():
        raise ValidationError(f"missing Bureau source snapshot schema: {path}")
    return _json_object(path.read_bytes(), str(path))


def _validate_snapshot(root: Path, snapshot: dict[str, Any]) -> None:
    _validate(snapshot, _snapshot_schema(root), "Bureau source snapshot")


def _render(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _preview(
    root: Path,
    snapshot: dict[str, Any],
    *,
    action: str,
    applied: bool,
    writes: list[str] | None = None,
) -> dict[str, Any]:
    target = root / SNAPSHOT_PATH
    rendered = _render(snapshot)
    existing = target.read_text(encoding="utf-8") if target.is_file() else None
    task_ids = [item["source_task_id"] for item in snapshot["tasks"]]
    return {
        "action": action,
        "source": snapshot["source"],
        "index": snapshot["index"],
        "schema": snapshot["schema"],
        "task_count": snapshot["task_count"],
        "status_counts": snapshot["status_counts"],
        "preview_task_ids": task_ids[:_PREVIEW_LIMIT],
        "preview_limit": _PREVIEW_LIMIT,
        "preview_truncated": len(task_ids) > _PREVIEW_LIMIT,
        "target": str(SNAPSHOT_PATH),
        "would_write": existing != rendered,
        "applied": applied,
        "writes": writes or [],
        "does_not_establish": list(_DOES_NOT_ESTABLISH),
    }


def source_check(
    root: str | Path,
    repository: str | Path,
    ref: str = "origin/main",
) -> dict[str, Any]:
    bureau_root = Path(root).expanduser().resolve()
    snapshot = build_snapshot(repository, ref)
    _validate_snapshot(bureau_root, snapshot)
    return _preview(bureau_root, snapshot, action="source-check", applied=False)


def source_sync(
    root: str | Path,
    repository: str | Path,
    ref: str = "origin/main",
    *,
    apply: bool = False,
) -> dict[str, Any]:
    bureau_root = Path(root).expanduser().resolve()
    snapshot = build_snapshot(repository, ref)
    _validate_snapshot(bureau_root, snapshot)
    if not apply:
        return _preview(bureau_root, snapshot, action="source-sync", applied=False)
    target = bureau_root / SNAPSHOT_PATH
    rendered = _render(snapshot)
    writes: list[str] = []
    if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
        atomic_write(target, rendered)
        writes.append(str(SNAPSHOT_PATH))
    persisted = _json_object(target.read_bytes(), str(target))
    _validate_snapshot(bureau_root, persisted)
    if canonical_json(persisted) != canonical_json(snapshot):
        raise ValidationError("persisted Weltgewebe source snapshot does not match its input")
    Registry.load(bureau_root)
    return _preview(
        bureau_root,
        snapshot,
        action="source-sync",
        applied=True,
        writes=writes,
    )
