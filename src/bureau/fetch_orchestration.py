"""Fail-closed fetch/import orchestration for Bureau.

The orchestration is deliberately two-phase. Planning is read-only and produces a
hash-bound description of the exact effect. Applying re-plans, verifies that hash,
checks runtime drift, then requires an approval bound to the plan digest before any
mutation starts.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from .approval import ApprovalEvidence, require_approval
from .legacy import Registry, StateError, ValidationError, sha256_json
from .repo_scan import _git_env, _git_read, _normalized_github_slug, scan_repository_registry
from .v2 import _runtime_execution_truth, runtime_drift_check
from .weltgewebe_source import source_sync

_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _runtime_gate(
    root: Path,
    *,
    state_db: Path | None = None,
    state_root: Path | None = None,
    runtime_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _runtime_execution_truth(
        runtime_drift_check(
            root,
            state_db=state_db,
            state_root=state_root,
            runtime_identity=runtime_identity,
        )
    )


def _digest_plan(plan: dict[str, Any]) -> dict[str, Any]:
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return {**unsigned, "plan_sha256": sha256_json(unsigned)}


def _conflict(
    *,
    code: str,
    message: str,
    repo: dict[str, Any],
    branch: str,
    source: dict[str, Any],
    required_human_decision: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "repo": repo,
        "branch": branch,
        "source": source,
        "required_human_decision": required_human_decision,
    }


def _run_git_write(
    repository: Path,
    arguments: list[str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
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
        return subprocess.run(
            command,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateError(f"Git mutation failed before a verified outcome: {exc}") from exc


def _git_error(result: subprocess.CompletedProcess[str], operation: str) -> StateError:
    detail = (result.stderr or "").strip().splitlines()
    suffix = detail[-1] if detail else f"exit {result.returncode}"
    return StateError(f"{operation} failed: {suffix}")


def _object_format(repository: Path) -> tuple[str, int]:
    result = _git_read(repository, "rev-parse", "--show-object-format=storage")
    if result.returncode:
        raise ValidationError(f"cannot determine Git object format for {repository}")
    name = result.stdout.strip().casefold()
    size = {"sha1": 40, "sha256": 64}.get(name)
    if size is None:
        raise ValidationError(f"unsupported Git object format for {repository}: {name or 'empty'}")
    return name, size


def _validate_oid(value: str, *, length: int, label: str) -> str:
    lowered = value.strip().casefold()
    if len(lowered) != length or not _HEX_RE.fullmatch(lowered):
        raise ValidationError(f"{label} is not one valid Git object id")
    return lowered


def _validate_branch(repository: Path, branch: str) -> str:
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise ValidationError(f"invalid branch {branch!r}")
    result = _git_read(repository, "check-ref-format", "--branch", branch)
    if result.returncode:
        raise ValidationError(f"invalid branch {branch!r}")
    return branch


def _validate_remote(remote: str) -> str:
    if not isinstance(remote, str) or not _REMOTE_RE.fullmatch(remote) or remote.startswith("-"):
        raise ValidationError(f"invalid remote {remote!r}")
    return remote


def _read_ref(repository: Path, ref: str, *, oid_length: int) -> str | None:
    result = _git_read(repository, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if result.returncode:
        return None
    return _validate_oid(result.stdout, length=oid_length, label=ref)


def _canonical_repo(
    registry: Registry,
    resource_id: str,
    *,
    discovery_registry_path: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    resource = registry.resources.get(resource_id)
    if resource is None:
        raise ValidationError(f"unknown repository resource {resource_id}")
    if resource.type != "git-repository":
        raise ValidationError(f"resource {resource_id} is not a git-repository")
    if not resource.path:
        raise ValidationError(f"repository resource {resource_id} has no canonical path")
    projection = scan_repository_registry(
        registry,
        discovery_registry_path=discovery_registry_path,
        resource_id=resource_id,
    )
    repositories = projection.get("repositories")
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise ValidationError(f"repository projection for {resource_id} is not singular")
    repo = repositories[0]
    if repo.get("status", {}).get("state") != "available":
        raise ValidationError(f"repository {resource_id} is not available")
    path = Path(resource.path).expanduser().resolve()
    return (
        {
            "resource_id": resource.id,
            "path": str(path),
            "github_slug": resource.github_slug,
            "grabowski_key": resource.grabowski_key,
        },
        path,
    )


def repo_fetch_plan(
    root: Path,
    registry: Registry,
    resource_id: str,
    *,
    branch: str = "main",
    remote: str = "origin",
    task_id: str | None = None,
    discovery_registry_path: Path | None = None,
    state_db: Path | None = None,
    state_root: Path | None = None,
    runtime_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a read-only, hash-bound plan for one remote-tracking fetch."""
    canonical, repository = _canonical_repo(
        registry, resource_id, discovery_registry_path=discovery_registry_path
    )
    branch = _validate_branch(repository, branch)
    remote = _validate_remote(remote)
    _, oid_length = _object_format(repository)

    remote_url_result = _git_read(repository, "remote", "get-url", remote)
    conflicts: list[dict[str, Any]] = []
    source = {"remote": remote, "ref": f"refs/heads/{branch}", "commit_sha": None}
    if remote_url_result.returncode:
        conflicts.append(
            _conflict(
                code="remote-missing",
                message=f"configured remote {remote} is unavailable",
                repo=canonical,
                branch=branch,
                source=source,
                required_human_decision="configure or select the intended remote",
            )
        )
        remote_url = None
    else:
        remote_url = remote_url_result.stdout.strip()
        configured_slug = _normalized_github_slug(remote_url)
        expected_slug = _normalized_github_slug(canonical.get("github_slug"))
        if expected_slug and configured_slug != expected_slug:
            conflicts.append(
                _conflict(
                    code="remote-identity-mismatch",
                    message="configured remote does not match the canonical repository identity",
                    repo=canonical,
                    branch=branch,
                    source={**source, "github_slug": configured_slug},
                    required_human_decision=(
                        f"choose a remote whose GitHub identity is exactly {expected_slug}"
                    ),
                )
            )

    remote_commit: str | None = None
    if not conflicts:
        remote_result = _git_read(
            repository,
            "ls-remote",
            "--exit-code",
            "--heads",
            remote,
            f"refs/heads/{branch}",
        )
        rows = [line.split() for line in remote_result.stdout.splitlines() if line.strip()]
        if remote_result.returncode or len(rows) != 1 or len(rows[0]) != 2:
            conflicts.append(
                _conflict(
                    code="source-ref-unavailable",
                    message="remote branch did not resolve to exactly one commit",
                    repo=canonical,
                    branch=branch,
                    source=source,
                    required_human_decision="confirm the intended remote branch and retry planning",
                )
            )
        else:
            remote_commit = _validate_oid(
                rows[0][0], length=oid_length, label=f"{remote}/{branch}"
            )
            source["commit_sha"] = remote_commit

    destination_ref = f"refs/remotes/{remote}/{branch}"
    before = _read_ref(repository, destination_ref, oid_length=oid_length)
    runtime = _runtime_gate(
        root,
        state_db=state_db,
        state_root=state_root,
        runtime_identity=runtime_identity,
    )
    if runtime.get("execution_blocked"):
        conflicts.append(
            _conflict(
                code="runtime-drift-blocked",
                message="Bureau runtime drift gate blocks repository mutation",
                repo=canonical,
                branch=branch,
                source=source,
                required_human_decision="resolve the reported runtime drift before fetching",
            )
        )

    plan = {
        "schema_version": 1,
        "kind": "bureau_repo_fetch_plan",
        "mode": "dry-run",
        "read_only": True,
        "task_id": task_id,
        "repo": canonical,
        "branch": branch,
        "source": source,
        "destination": {"ref": destination_ref, "before_commit": before},
        "runtime": runtime,
        "approval": {"action_class": "repository_mutation", "required_level": "operator"},
        "proposed_actions": [
            {
                "action": "fetch-to-temporary-ref",
                "remote": remote,
                "source_ref": f"refs/heads/{branch}",
                "expected_commit": remote_commit,
            },
            {
                "action": "compare-and-swap-remote-tracking-ref",
                "destination_ref": destination_ref,
                "expected_before": before,
                "after": remote_commit,
            },
        ],
        "conflicts": conflicts,
        "allowed": not conflicts,
        "does_not_mutate": ["HEAD", "current_branch", "index", "worktree"],
    }
    return _digest_plan(plan)


def _cleanup_temp_ref(repository: Path, temp_ref: str, observed: str | None) -> None:
    if not observed:
        return
    result = _run_git_write(repository, ["update-ref", "-d", temp_ref, observed])
    if result.returncode:
        raise _git_error(result, "temporary fetch ref cleanup")


def apply_repo_fetch_plan(
    root: Path,
    registry: Registry,
    resource_id: str,
    *,
    expected_plan_sha256: str,
    approval: ApprovalEvidence | None,
    branch: str = "main",
    remote: str = "origin",
    task_id: str | None = None,
    discovery_registry_path: Path | None = None,
    state_db: Path | None = None,
    state_root: Path | None = None,
    runtime_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one exact fetch plan without touching HEAD/index/worktree."""
    plan = repo_fetch_plan(
        root,
        registry,
        resource_id,
        branch=branch,
        remote=remote,
        task_id=task_id,
        discovery_registry_path=discovery_registry_path,
        state_db=state_db,
        state_root=state_root,
        runtime_identity=runtime_identity,
    )
    if plan["plan_sha256"] != expected_plan_sha256:
        raise StateError("fetch plan drifted; generate and review a fresh dry-run plan")
    if not plan["allowed"]:
        raise StateError("fetch plan is blocked by precondition conflicts")
    approval_decision = require_approval(
        "repository_mutation",
        approval,
        expected_reference=expected_plan_sha256,
        task_id=task_id,
    )

    repository = Path(plan["repo"]["path"])
    _, oid_length = _object_format(repository)
    source_commit = str(plan["source"]["commit_sha"])
    destination_ref = str(plan["destination"]["ref"])
    before = plan["destination"]["before_commit"]
    if source_commit == before:
        receipt = {
            "schema_version": 1,
            "kind": "bureau_repo_fetch_receipt",
            "status": "no-op",
            "plan_sha256": expected_plan_sha256,
            "repo": plan["repo"],
            "branch": branch,
            "source": plan["source"],
            "destination": {
                "ref": destination_ref,
                "before_commit": before,
                "after_commit": before,
            },
            "approval": approval_decision,
            "changed": False,
        }
        return {**receipt, "receipt_sha256": sha256_json(receipt)}

    token = hashlib.sha256(
        f"{resource_id}\0{remote}\0{branch}\0{source_commit}".encode()
    ).hexdigest()[:24]
    temp_ref = f"refs/bureau/fetch/{token}"
    if _read_ref(repository, temp_ref, oid_length=oid_length) is not None:
        raise StateError(f"temporary fetch ref already exists: {temp_ref}")

    fetch = _run_git_write(
        repository,
        [
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            "--refmap=",
            remote,
            f"refs/heads/{branch}:{temp_ref}",
        ],
    )
    if fetch.returncode:
        raise _git_error(fetch, "fetch into temporary ref")

    fetched = _read_ref(repository, temp_ref, oid_length=oid_length)
    if fetched != source_commit:
        _cleanup_temp_ref(repository, temp_ref, fetched)
        conflict = _conflict(
            code="source-moved-during-fetch",
            message="remote branch changed after the reviewed dry-run",
            repo=plan["repo"],
            branch=branch,
            source={**plan["source"], "observed_commit": fetched},
            required_human_decision="review a fresh dry-run plan for the new source commit",
        )
        return {
            "schema_version": 1,
            "kind": "bureau_repo_fetch_receipt",
            "status": "conflict",
            "plan_sha256": expected_plan_sha256,
            "conflict": conflict,
            "effect_started": True,
            "destination_changed": False,
        }

    current_destination = _read_ref(repository, destination_ref, oid_length=oid_length)
    if current_destination != before:
        _cleanup_temp_ref(repository, temp_ref, fetched)
        conflict = _conflict(
            code="destination-ref-drift",
            message="local remote-tracking ref changed after the reviewed dry-run",
            repo=plan["repo"],
            branch=branch,
            source=plan["source"],
            required_human_decision=(
                "review a fresh dry-run plan against the current destination ref"
            ),
        )
        return {
            "schema_version": 1,
            "kind": "bureau_repo_fetch_receipt",
            "status": "conflict",
            "plan_sha256": expected_plan_sha256,
            "conflict": conflict,
            "effect_started": True,
            "destination_changed": False,
        }

    if before:
        ff = _git_read(repository, "merge-base", "--is-ancestor", before, source_commit)
        if ff.returncode != 0:
            _cleanup_temp_ref(repository, temp_ref, fetched)
            conflict = _conflict(
                code="non-fast-forward",
                message="remote branch is not a descendant of the current remote-tracking ref",
                repo=plan["repo"],
                branch=branch,
                source=plan["source"],
                required_human_decision=(
                    "decide whether the remote rewrite is legitimate; "
                    "Bureau will not force-update it"
                ),
            )
            return {
                "schema_version": 1,
                "kind": "bureau_repo_fetch_receipt",
                "status": "conflict",
                "plan_sha256": expected_plan_sha256,
                "conflict": conflict,
                "effect_started": True,
                "destination_changed": False,
            }

    commands = ["start"]
    if before:
        commands.append(f"update {destination_ref} {source_commit} {before}")
    else:
        commands.append(f"create {destination_ref} {source_commit}")
    commands.extend([f"delete {temp_ref} {source_commit}", "prepare", "commit", ""])
    update = _run_git_write(repository, ["update-ref", "--stdin"], input_text="\n".join(commands))
    if update.returncode:
        # The transaction is all-or-nothing. Read back before attempting cleanup.
        observed_destination = _read_ref(repository, destination_ref, oid_length=oid_length)
        observed_temp = _read_ref(repository, temp_ref, oid_length=oid_length)
        if observed_destination == before and observed_temp == source_commit:
            _cleanup_temp_ref(repository, temp_ref, observed_temp)
        raise _git_error(update, "atomic remote-tracking ref update")

    after = _read_ref(repository, destination_ref, oid_length=oid_length)
    temp_after = _read_ref(repository, temp_ref, oid_length=oid_length)
    if after != source_commit or temp_after is not None:
        raise StateError("post-fetch readback did not establish the intended atomic ref state")
    receipt = {
        "schema_version": 1,
        "kind": "bureau_repo_fetch_receipt",
        "status": "applied",
        "plan_sha256": expected_plan_sha256,
        "repo": plan["repo"],
        "branch": branch,
        "source": plan["source"],
        "destination": {"ref": destination_ref, "before_commit": before, "after_commit": after},
        "approval": approval_decision,
        "changed": True,
        "worktree_mutated": False,
    }
    return {**receipt, "receipt_sha256": sha256_json(receipt)}


def source_import_plan(
    root: Path,
    repository: str | Path,
    ref: str = "origin/main",
    *,
    task_id: str | None = None,
    state_db: Path | None = None,
    state_root: Path | None = None,
    runtime_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a read-only, provenance-rich plan for Weltgewebe source import."""
    preview = source_sync(root, repository, ref, apply=False)
    runtime = _runtime_gate(
        root,
        state_db=state_db,
        state_root=state_root,
        runtime_identity=runtime_identity,
    )
    conflicts: list[dict[str, Any]] = []
    source = {
        "name": "weltgewebe",
        "repository": str(Path(repository).expanduser().resolve()),
        "ref": ref,
        "commit_sha": preview["commit_sha"],
        "index_sha256": preview["index_sha256"],
        "schema_sha256": preview["schema_sha256"],
    }
    repo = {"resource_id": "source.weltgewebe", "path": source["repository"]}
    if runtime.get("execution_blocked"):
        conflicts.append(
            _conflict(
                code="runtime-drift-blocked",
                message="Bureau runtime drift gate blocks source import",
                repo=repo,
                branch=ref,
                source=source,
                required_human_decision="resolve the reported runtime drift before importing",
            )
        )
    plan = {
        "schema_version": 1,
        "kind": "bureau_source_import_plan",
        "mode": "dry-run",
        "read_only": True,
        "task_id": task_id,
        "repo": repo,
        "branch": ref,
        "source": source,
        "target": preview["target"],
        "document_sha256": preview["document_sha256"],
        "changed": preview["changed"],
        "changes": preview["changes"],
        "runtime": runtime,
        "approval": {"action_class": "source_import", "required_level": "reviewed_receipt"},
        "proposed_actions": [
            {
                "action": "import-reviewed-source-snapshot",
                "source_commit": preview["commit_sha"],
                "target": preview["target"],
                "document_sha256": preview["document_sha256"],
            }
        ],
        "conflicts": conflicts,
        "allowed": not conflicts,
    }
    return _digest_plan(plan)


def apply_source_import_plan(
    root: Path,
    repository: str | Path,
    ref: str = "origin/main",
    *,
    expected_plan_sha256: str,
    approval: ApprovalEvidence | None,
    task_id: str | None = None,
    state_db: Path | None = None,
    state_root: Path | None = None,
    runtime_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one exact reviewed source-import plan."""
    plan = source_import_plan(
        root,
        repository,
        ref,
        task_id=task_id,
        state_db=state_db,
        state_root=state_root,
        runtime_identity=runtime_identity,
    )
    if plan["plan_sha256"] != expected_plan_sha256:
        raise StateError("source import plan drifted; generate and review a fresh dry-run plan")
    if not plan["allowed"]:
        raise StateError("source import plan is blocked by precondition conflicts")
    approval_decision = require_approval(
        "source_import",
        approval,
        expected_reference=expected_plan_sha256,
        task_id=task_id,
    )
    applied = source_sync(
        root,
        repository,
        ref,
        apply=True,
        expected_commit_sha=str(plan["source"]["commit_sha"]),
    )
    if applied["document_sha256"] != plan["document_sha256"]:
        raise StateError("source import write readback does not match the reviewed document digest")
    receipt = {
        "schema_version": 1,
        "kind": "bureau_source_import_receipt",
        "status": "applied" if applied["applied"] else "no-op",
        "plan_sha256": expected_plan_sha256,
        "source": plan["source"],
        "target": plan["target"],
        "document_sha256": applied["document_sha256"],
        "approval": approval_decision,
        "changed": applied["changed"],
        "applied": applied["applied"],
    }
    return {**receipt, "receipt_sha256": sha256_json(receipt)}
