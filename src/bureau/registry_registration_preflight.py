from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .acceptance import AcceptanceContractError
from .approval import validate_declared_task_approval
from .core import StateError
from .github_repository import validate_github_repository_slug
from .lease_contract import assess_task_broad_bureau_scope
from .schema_validation import DocumentSchemaError, SchemaSet, default_schema_set

REGISTRATION_PREFLIGHT_SCHEMA_VERSION = 1
REGISTRATION_PREFLIGHT_KIND = "bureau_registry_registration_preflight"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TASK_PATH_RE = re.compile(r"^registry/tasks/([A-Za-z0-9][A-Za-z0-9._:-]{0,239})\.json$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9ÄÖÜäöüß]+")
_STOPWORDS = {
    "aber",
    "alle",
    "also",
    "and",
    "auch",
    "aus",
    "bei",
    "den",
    "der",
    "die",
    "eine",
    "für",
    "mit",
    "oder",
    "ohne",
    "the",
    "und",
    "von",
    "vor",
    "wird",
    "werden",
    "with",
    "zum",
    "zur",
}

OpenPrProvider = Callable[[str, int | None], list[dict[str, Any]]]
BaseShaProvider = Callable[[Path, str], str]


class RegistrationPreflightError(StateError):
    """Fail-closed input or observation error for permanent Registry registration."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decision_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_task_id(task_id: str) -> str:
    normalized = task_id.strip() if isinstance(task_id, str) else ""
    if not _TASK_ID_RE.fullmatch(normalized):
        raise RegistrationPreflightError("invalid permanent Registry task id")
    return normalized


def task_path_for_id(task_id: str) -> str:
    return f"registry/tasks/{validate_task_id(task_id)}.json"


def validate_task_path(task_id: str, task_path: str) -> str:
    expected = task_path_for_id(task_id)
    match = _TASK_PATH_RE.fullmatch(task_path) if isinstance(task_path, str) else None
    if task_path != expected or match is None or match.group(1) != task_id:
        raise RegistrationPreflightError(f"task_path must be exactly {expected}")
    return task_path


def validate_sha(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise RegistrationPreflightError(f"{field} must be a lowercase 40-character Git revision")
    return value


def _task_text(task: dict[str, Any]) -> str:
    parts = [task.get("title"), task.get("goal"), task.get("summary")]
    return " ".join(str(value) for value in parts if isinstance(value, str) and value.strip())


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(value)
        if len(token) >= 4 and token.casefold() not in _STOPWORDS
    }


def _semantic_score(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, list[str]]:
    left_tokens = _tokens(_task_text(left))
    right_tokens = _tokens(_task_text(right))
    if not left_tokens or not right_tokens:
        return 0.0, []
    common = sorted(left_tokens & right_tokens)
    if len(common) < 3:
        return 0.0, common
    return len(common) / min(len(left_tokens), len(right_tokens)), common


def _semantic_hints(
    proposed_task: dict[str, Any],
    canonical_tasks: list[dict[str, Any]],
    open_prs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    proposed_id = str(proposed_task["id"])
    candidates: list[tuple[str, int | None, dict[str, Any]]] = [
        ("canonical_main", None, task) for task in canonical_tasks
    ]
    for pr in open_prs:
        for task in pr.get("tasks", []):
            if isinstance(task, dict):
                candidates.append(("open_pr", pr.get("number"), task))
    for source, pr_number, task in candidates:
        task_id = task.get("id")
        if not isinstance(task_id, str) or task_id == proposed_id:
            continue
        score, common = _semantic_score(proposed_task, task)
        if score < 0.6:
            continue
        item: dict[str, Any] = {
            "kind": "possible_duplicate",
            "source": source,
            "task_id": task_id,
            "path": task_path_for_id(task_id),
            "score": round(score, 6),
            "common_tokens": common[:12],
        }
        if pr_number is not None:
            item["pr_number"] = pr_number
        hints.append(item)
    return sorted(
        hints,
        key=lambda item: (
            str(item["source"]),
            int(item.get("pr_number") or 0),
            str(item["task_id"]),
        ),
    )


def evaluate_registration_preflight(
    *,
    repository: str,
    proposed_task: dict[str, Any],
    proposed_path: str,
    checked_base_sha: str,
    current_base_sha_before: str,
    current_base_sha_after: str,
    canonical_tasks: list[dict[str, Any]],
    open_prs: list[dict[str, Any]],
    pr_number: int | None = None,
    head_sha: str | None = None,
    schemas: SchemaSet | None = None,
) -> dict[str, Any]:
    repository = validate_github_repository_slug(repository)
    task_id = validate_task_id(str(proposed_task.get("id", "")))
    proposed_path = validate_task_path(task_id, proposed_path)
    checked_base_sha = validate_sha(checked_base_sha, field="checked_base_sha")
    current_base_sha_before = validate_sha(current_base_sha_before, field="current_base_sha_before")
    current_base_sha_after = validate_sha(current_base_sha_after, field="current_base_sha_after")
    if head_sha is not None:
        head_sha = validate_sha(head_sha, field="head_sha")
    if pr_number is not None and (not isinstance(pr_number, int) or pr_number <= 0):
        raise RegistrationPreflightError("pr_number must be a positive integer")

    task_schema_errors: list[str] = []
    acceptance_contract_errors: list[dict[str, Any]] = []
    try:
        (schemas or default_schema_set()).validate_task_write(proposed_task, proposed_path)
    except DocumentSchemaError as exc:
        task_schema_errors = str(exc).splitlines()
    except AcceptanceContractError as exc:
        acceptance_contract_errors = exc.diagnostics

    collisions: list[dict[str, Any]] = []
    canonical_ids = {
        str(task.get("id"))
        for task in canonical_tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    if task_id in canonical_ids:
        collisions.append(
            {"source": "canonical_main", "task_id": task_id, "path": proposed_path}
        )
    for pr in open_prs:
        number = pr.get("number")
        if pr_number is not None and number == pr_number:
            continue
        paths = {
            str(path)
            for path in pr.get("task_paths", [])
            if isinstance(path, str) and _TASK_PATH_RE.fullmatch(path)
        }
        ids = {
            match.group(1)
            for path in paths
            if (match := _TASK_PATH_RE.fullmatch(path)) is not None
        }
        if proposed_path in paths or task_id in ids:
            collisions.append(
                {
                    "source": "open_pr",
                    "pr_number": number,
                    "head_sha": pr.get("head_sha"),
                    "task_id": task_id,
                    "path": proposed_path,
                }
            )
    collisions = sorted(
        collisions,
        key=lambda item: (
            str(item["source"]),
            int(item.get("pr_number") or 0),
            str(item["path"]),
        ),
    )

    approval_contract_errors = validate_declared_task_approval(proposed_task)

    reasons: list[str] = []
    if checked_base_sha != current_base_sha_before:
        reasons.append("stale_base")
    if current_base_sha_before != current_base_sha_after:
        reasons.append("base_changed_during_preflight")
    if collisions:
        reasons.append("registry_collision")
    broad_scope_assessment = assess_task_broad_bureau_scope(proposed_task)
    if (
        broad_scope_assessment["broad_scope_requested"]
        and not broad_scope_assessment["allowed"]
    ):
        reasons.append("broad_bureau_scope")
    if approval_contract_errors:
        reasons.append("approval_contract_invalid")
    if task_schema_errors:
        reasons.append("task_schema_invalid")
    if acceptance_contract_errors:
        reasons.append("acceptance_contract_invalid")
    decision = "allow" if not reasons else "block"
    receipt: dict[str, Any] = {
        "schema_version": REGISTRATION_PREFLIGHT_SCHEMA_VERSION,
        "kind": REGISTRATION_PREFLIGHT_KIND,
        "repository": repository,
        "checked_base_sha": checked_base_sha,
        "current_base_sha_before": current_base_sha_before,
        "current_base_sha_after": current_base_sha_after,
        "proposed": {
            "task_id": task_id,
            "path": proposed_path,
            "title": proposed_task.get("title"),
            "goal": proposed_task.get("goal"),
        },
        "pr_identity": {"number": pr_number, "head_sha": head_sha},
        "collisions": collisions,
        "approval_contract_errors": approval_contract_errors,
        "task_schema_errors": task_schema_errors,
        "acceptance_contract_errors": acceptance_contract_errors,
        "semantic_hints": _semantic_hints(proposed_task, canonical_tasks, open_prs),
        "broad_bureau_scope": broad_scope_assessment,
        "decision": decision,
        "reasons": reasons or ["registration_slot_available"],
    }
    receipt["decision_sha256"] = decision_digest(receipt)
    return receipt


def _run_text(arguments: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        arguments,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RegistrationPreflightError(f"command failed: {' '.join(arguments)}: {detail}")
    return result.stdout.strip()


def remote_base_sha(root: Path, base_ref: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_ref) or ".." in base_ref:
        raise RegistrationPreflightError("base_ref contains unsupported characters")
    output = _run_text(["git", "ls-remote", "origin", f"refs/heads/{base_ref}"], cwd=root)
    parts = output.split()
    if len(parts) < 2:
        raise RegistrationPreflightError(f"cannot resolve remote base ref {base_ref}")
    return validate_sha(parts[0], field="current_base_sha")


def canonical_tasks_at_revision(root: Path, revision: str) -> list[dict[str, Any]]:
    revision = validate_sha(revision, field="revision")
    paths = _run_text(
        ["git", "ls-tree", "-r", "--name-only", revision, "--", "registry/tasks"],
        cwd=root,
    ).splitlines()
    tasks: list[dict[str, Any]] = []
    for path in sorted(path for path in paths if _TASK_PATH_RE.fullmatch(path)):
        raw = _run_text(["git", "show", f"{revision}:{path}"], cwd=root)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RegistrationPreflightError(
                f"canonical task JSON is invalid at {path}: {exc}"
            ) from exc
        if isinstance(value, dict):
            tasks.append(value)
    return tasks


def _decode_gh_content(value: dict[str, Any]) -> dict[str, Any] | None:
    content = value.get("content")
    if not isinstance(content, str):
        return None
    try:
        decoded = base64.b64decode(content.replace("\n", "")).decode("utf-8")
        task = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return task if isinstance(task, dict) else None


def github_open_prs(
    repository: str,
    current_pr_number: int | None = None,
    *,
    runner: Callable[[list[str]], str] | None = None,
) -> list[dict[str, Any]]:
    repository = validate_github_repository_slug(repository)
    run = runner or _run_text
    raw = run(
        [
            "gh",
            "api",
            f"repos/{repository}/pulls?state=open&per_page=100",
            "--paginate",
            "--jq",
            ".[] | [.number, .head.sha] | @tsv",
        ]
    )
    listed: list[tuple[int, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            raise RegistrationPreflightError("cannot parse paginated open PR listing")
        try:
            number = int(parts[0])
        except ValueError as exc:
            raise RegistrationPreflightError("open PR number is not an integer") from exc
        head_sha = validate_sha(parts[1], field="open_pr_head_sha")
        if number <= 0:
            raise RegistrationPreflightError("open PR number must be positive")
        listed.append((number, head_sha))

    result: list[dict[str, Any]] = []
    for number, head_sha in listed:
        if number == current_pr_number:
            continue
        files_raw = run(
            [
                "gh",
                "api",
                f"repos/{repository}/pulls/{number}/files?per_page=100",
                "--paginate",
                "--jq",
                ".[].filename",
            ]
        )
        task_paths = sorted(
            {
                path
                for path in files_raw.splitlines()
                if _TASK_PATH_RE.fullmatch(path)
            }
        )
        tasks: list[dict[str, Any]] = []
        for path in task_paths:
            try:
                content = json.loads(
                    run(
                        [
                            "gh",
                            "api",
                            f"repos/{repository}/contents/{path}?ref={head_sha}",
                        ]
                    )
                )
            except (RegistrationPreflightError, json.JSONDecodeError):
                continue
            task = _decode_gh_content(content) if isinstance(content, dict) else None
            if task is not None:
                tasks.append(task)
        result.append(
            {
                "number": number,
                "head_sha": head_sha,
                "task_paths": task_paths,
                "tasks": tasks,
            }
        )
    return sorted(result, key=lambda item: int(item["number"]))


def _github_repository_provider_identity(
    repository: str,
    run: Callable[[list[str]], str],
) -> dict[str, Any]:
    try:
        raw = run([
            "gh",
            "api",
            f"repos/{repository}",
            "--jq",
            "[.id, .full_name] | @tsv",
        ])
    except Exception as exc:
        raise RegistrationPreflightError(
            "cannot resolve stable GitHub repository identity"
        ) from exc
    parts = raw.strip().split("\t")
    if len(parts) != 2:
        raise RegistrationPreflightError(
            "cannot parse stable GitHub repository identity"
        )
    try:
        repository_id = int(parts[0])
    except ValueError as exc:
        raise RegistrationPreflightError(
            "GitHub repository id is not an integer"
        ) from exc
    if repository_id <= 0:
        raise RegistrationPreflightError("GitHub repository id must be positive")
    return {
        "repository_id": repository_id,
        "canonical_repository": validate_github_repository_slug(parts[1].strip()),
    }


def _github_open_pr_identity_listing(
    repository: str,
    run: Callable[[list[str]], str],
) -> list[dict[str, Any]]:
    try:
        raw = run([
            "gh", "api", f"repos/{repository}/pulls?state=open&per_page=100",
            "--paginate", "--jq",
            ".[] | select(.base.ref == \"main\") "
            "| [.number, .head.sha, .head.ref, (.head.repo.id // \"\"), "
            "(.head.repo.full_name // \"\"), .base.sha, .base.ref] | @tsv",
        ])
    except Exception as exc:
        raise RegistrationPreflightError("cannot read complete open PR identity listing") from exc
    listed: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            raise RegistrationPreflightError("cannot parse paginated open PR identity listing")
        try:
            number = int(parts[0])
        except ValueError as exc:
            raise RegistrationPreflightError("open PR number is not an integer") from exc
        if number <= 0 or number in seen_numbers:
            raise RegistrationPreflightError(
                "open PR listing contains an invalid or duplicate number"
            )
        seen_numbers.add(number)
        head_ref_name = parts[2].strip()
        head_repository_id_raw = parts[3].strip()
        if head_repository_id_raw:
            try:
                head_repository_id = int(head_repository_id_raw)
            except ValueError as exc:
                raise RegistrationPreflightError(
                    f"open PR #{number} head repository id is not an integer"
                ) from exc
            if head_repository_id <= 0:
                raise RegistrationPreflightError(
                    f"open PR #{number} head repository id must be positive"
                )
        else:
            head_repository_id = None
        head_repository_raw = parts[4].strip()
        head_repository = (
            validate_github_repository_slug(head_repository_raw)
            if head_repository_raw
            else None
        )
        base_ref_name = parts[6].strip()
        if not head_ref_name or not base_ref_name:
            raise RegistrationPreflightError("open PR listing is missing head/base ref identity")
        if base_ref_name != "main":
            continue
        listed.append({
            "number": number,
            "head_sha": validate_sha(parts[1], field="open_pr_head_sha"),
            "head_ref_name": head_ref_name,
            "head_repository_id": head_repository_id,
            "head_repository": head_repository,
            "base_sha": validate_sha(parts[5], field="open_pr_base_sha"),
            "base_ref_name": base_ref_name,
        })
    return sorted(listed, key=lambda item: int(item["number"]))


def github_open_pr_identity_observation(
    repository: str,
    *,
    checked_base_sha: str,
    current_pr_number: int | None = None,
    runner: Callable[[list[str]], str] | None = None,
    observed_at_unix: int | None = None,
) -> dict[str, Any]:
    """Read a stable, revision-bound exact Registry identity view from open GitHub PRs."""
    repository = validate_github_repository_slug(repository)
    checked_base_sha = validate_sha(checked_base_sha, field="checked_base_sha")
    run = runner or _run_text
    repository_identity_before = _github_repository_provider_identity(repository, run)
    listed_before = _github_open_pr_identity_listing(repository, run)
    open_prs: list[dict[str, Any]] = []
    for identity in listed_before:
        number = int(identity["number"])
        if number == current_pr_number:
            continue
        if identity["base_ref_name"] != "main":
            continue
        try:
            files_raw = run([
                "gh", "api", f"repos/{repository}/pulls/{number}/files?per_page=100",
                "--paginate", "--jq", ".[].filename",
            ])
        except Exception as exc:
            raise RegistrationPreflightError(
                f"cannot read complete file listing for open PR #{number}"
            ) from exc
        task_paths = sorted(
            {path for path in files_raw.splitlines() if _TASK_PATH_RE.fullmatch(path)}
        )
        tasks: list[dict[str, Any]] = []
        task_ids: list[str] = []
        for path in task_paths:
            try:
                head_repository = identity.get("head_repository")
                head_repository_id = identity.get("head_repository_id")
                if not head_repository or not isinstance(head_repository_id, int):
                    raise RegistrationPreflightError(
                        f"open PR #{number} is missing head repository identity"
                    )
                content_raw = run([
                    "gh",
                    "api",
                    f"repos/{head_repository}/contents/{path}?ref={identity['head_sha']}",
                ])
                content = json.loads(content_raw)
            except Exception as exc:
                raise RegistrationPreflightError(
                    f"cannot read exact Registry task identity {path} from open PR #{number}"
                ) from exc
            task = _decode_gh_content(content) if isinstance(content, dict) else None
            if task is None:
                raise RegistrationPreflightError(
                    f"open PR #{number} Registry task content is incomplete or invalid at {path}"
                )
            task_id = validate_task_id(str(task.get("id", "")))
            if task_path_for_id(task_id) != path:
                raise RegistrationPreflightError(
                    f"open PR #{number} Registry task id/path identity is contradictory at {path}"
                )
            tasks.append(task)
            task_ids.append(task_id)
        open_prs.append({
            **identity,
            "task_paths": task_paths,
            "task_ids": sorted(set(task_ids)),
            "tasks": tasks,
        })
    listed_after = _github_open_pr_identity_listing(repository, run)
    repository_identity_after = _github_repository_provider_identity(repository, run)
    if listed_after != listed_before:
        raise RegistrationPreflightError("open PR identity listing changed during observation")
    if repository_identity_after != repository_identity_before:
        raise RegistrationPreflightError(
            "GitHub repository identity changed during observation"
        )
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "kind": "bureau_registry_open_pr_identity_observation",
        "repository": repository,
        **repository_identity_before,
        "checked_base_sha": checked_base_sha,
        "observed_at_unix": int(time.time()) if observed_at_unix is None else int(observed_at_unix),
        "complete": True,
        "open_prs": open_prs,
        "does_not_establish": [
            "queue_truth", "task_readiness", "claim_or_dispatch_authority",
            "merge_or_deployment_authority", "semantic_duplicate_identity",
        ],
    }
    return {**unsigned, "observation_sha256": decision_digest(unsigned)}


def exact_open_pr_identity_collisions(
    observation: dict[str, Any],
    *,
    task_id: str,
    target_path: str,
    excluded_head_ref: str | None = None,
    excluded_head_repository_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return only exact task-id/path collisions; semantic similarity is not consulted."""
    task_id = validate_task_id(task_id)
    target_path = validate_task_path(task_id, target_path)
    if excluded_head_repository_id is not None and (
        not isinstance(excluded_head_repository_id, int)
        or isinstance(excluded_head_repository_id, bool)
        or excluded_head_repository_id <= 0
    ):
        raise RegistrationPreflightError(
            "excluded head repository id must be a positive integer"
        )
    if (
        not isinstance(observation, dict)
        or observation.get("kind") != "bureau_registry_open_pr_identity_observation"
        or observation.get("complete") is not True
        or not isinstance(observation.get("open_prs"), list)
    ):
        raise RegistrationPreflightError("open PR identity observation is incomplete")
    collisions: list[dict[str, Any]] = []
    for pr in observation["open_prs"]:
        if not isinstance(pr, dict):
            raise RegistrationPreflightError(
                "open PR identity observation contains invalid entries"
            )
        if pr.get("base_ref_name") != "main":
            continue
        if (
            excluded_head_ref
            and excluded_head_repository_id is not None
            and pr.get("head_ref_name") == excluded_head_ref
            and pr.get("head_repository_id") == excluded_head_repository_id
        ):
            continue
        reasons: list[str] = []
        if target_path in pr.get("task_paths", []):
            reasons.append("target_path")
        if task_id in pr.get("task_ids", []):
            reasons.append("task_id")
        if reasons:
            collisions.append({
                "pr_number": pr.get("number"),
                "head_sha": pr.get("head_sha"),
                "head_ref_name": pr.get("head_ref_name"),
                "head_repository_id": pr.get("head_repository_id"),
                "head_repository": pr.get("head_repository"),
                "base_sha": pr.get("base_sha"),
                "base_ref_name": pr.get("base_ref_name"),
                "task_paths": list(pr.get("task_paths", [])),
                "task_ids": list(pr.get("task_ids", [])),
                "collision_axes": reasons,
            })
    return collisions


def repository_registration_preflight(
    root: str | Path,
    *,
    repository: str,
    task_json_path: str | Path,
    checked_base_sha: str,
    base_ref: str = "main",
    pr_number: int | None = None,
    head_sha: str | None = None,
    open_pr_provider: OpenPrProvider | None = None,
    base_sha_provider: BaseShaProvider | None = None,
) -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    task_file = Path(task_json_path).expanduser()
    if not task_file.is_absolute():
        task_file = root_path / task_file
    task_file = task_file.resolve()
    try:
        relative = task_file.relative_to(root_path).as_posix()
    except ValueError as exc:
        raise RegistrationPreflightError("task_json_path must be inside the Registry root") from exc
    try:
        proposed_task = json.loads(task_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationPreflightError(f"cannot read proposed task JSON: {exc}") from exc
    if not isinstance(proposed_task, dict):
        raise RegistrationPreflightError("proposed task JSON must be an object")
    task_id = validate_task_id(str(proposed_task.get("id", "")))
    validate_task_path(task_id, relative)
    checked_base_sha = validate_sha(checked_base_sha, field="checked_base_sha")
    base_provider = base_sha_provider or remote_base_sha
    open_provider = open_pr_provider or github_open_prs
    current_before = base_provider(root_path, base_ref)
    canonical_tasks = canonical_tasks_at_revision(root_path, checked_base_sha)
    open_prs = open_provider(repository, pr_number)
    current_after = base_provider(root_path, base_ref)
    return evaluate_registration_preflight(
        repository=repository,
        proposed_task=proposed_task,
        proposed_path=relative,
        checked_base_sha=checked_base_sha,
        current_base_sha_before=current_before,
        current_base_sha_after=current_after,
        canonical_tasks=canonical_tasks,
        open_prs=open_prs,
        pr_number=pr_number,
        head_sha=head_sha,
        schemas=SchemaSet(root_path / "schemas"),
    )


def write_receipt(path: str | Path, receipt: dict[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
