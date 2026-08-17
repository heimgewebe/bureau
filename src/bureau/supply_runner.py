"""Close the claimable-supply loop between the canonical dispatcher and the Registry.

`bureau.task_supply` can only preview: it demands an already authoritative frontier
snapshot bound to an exact Registry revision. Nothing produced that snapshot, so the
supply report the agent frontier reads never existed and a starved claimable frontier
stayed a read-only observation. This stage observes the authoritative frontier through
the canonical dispatcher, persists it as a revision-bound snapshot, writes the supply
report to the path `bureau-agent-frontier` already consumes, and publishes the bounded
fallback plan only when explicit mutation authority is granted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .core import Dispatcher
from .cycle_contract import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    atomic_json,
    cycle_id,
    utc_now,
    validate_receipt,
)
from .read_only_state import ReadOnlyStateStore
from .task_specs import task_spec_digest
from .task_supply import (
    SupplyError,
    SupplyPolicy,
    _fsync_directory,
    _git_head,
    build_registry_supply_report,
    file_sha256,
    publish_supply_plan,
    sha256_json,
)
from .v2 import Registry, StateStore

CYCLE_SCHEMA_VERSION = 1
CYCLE_KIND = "bureau_task_supply_cycle_result"
SNAPSHOT_KIND = "bureau_authoritative_frontier_snapshot"
GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
SNAPSHOT_FIELDS = (
    "task_id",
    "title",
    "effective_state",
    "queue_lane",
    "eligible",
    "closure_bridge",
    "claim_reasons",
    "reasons",
)


def default_state_root() -> Path:
    return Path(
        os.environ.get(
            "BUREAU_TASK_SUPPLY_STATE_ROOT",
            Path.home() / ".local/state/bureau-task-supply",
        )
    ).expanduser()


def report_path(state_root: Path) -> Path:
    return state_root / "latest-report.json"


def snapshot_path(state_root: Path) -> Path:
    return state_root / "frontier-snapshot.json"


@dataclass(frozen=True)
class FrontierObservation:
    """One authoritative, revision-bound dispatcher observation."""

    frontier: tuple[dict[str, Any], ...]
    runtime_healthy: bool
    runtime_blocker_codes: tuple[str, ...]
    capabilities: tuple[str, ...]


def _projected_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in SNAPSHOT_FIELDS if key in item}


def observe_authoritative_frontier(
    *,
    registry_root: Path,
    capabilities: Sequence[str],
    state_db: Path | None = None,
    state_store_root: Path | None = None,
) -> FrontierObservation:
    """Read the frontier the canonical claim path would evaluate, with its runtime gate."""
    selected = tuple(sorted({str(item) for item in capabilities if str(item)}))
    if not selected:
        raise SupplyError("at least one worker capability is required")
    registry = Registry.load(registry_root)
    store = ReadOnlyStateStore(state_db, state_store_root)
    dispatcher = Dispatcher(registry, store)
    # Same truth the claim path gates on, so supply health cannot diverge from dispatch.
    runtime_truth = dispatcher._runtime_execution_truth()
    frontier = dispatcher.frontier(set(selected))
    return FrontierObservation(
        frontier=tuple(_projected_item(item) for item in frontier),
        runtime_healthy=runtime_truth.get("execution_blocked") is not True,
        runtime_blocker_codes=tuple(
            str(code) for code in runtime_truth.get("blocker_codes", []) if code
        ),
        capabilities=selected,
    )


def _write_snapshot(
    path: Path,
    observation: FrontierObservation,
    *,
    registry_root: Path,
    registry_head: str,
    queue_sha256: str,
    generated_at: str,
) -> str:
    atomic_json(
        path,
        {
            "schema_version": CYCLE_SCHEMA_VERSION,
            "kind": SNAPSHOT_KIND,
            "generated_at": generated_at,
            "registry": {
                "root": str(registry_root),
                "head": registry_head,
                "queue_sha256": queue_sha256,
            },
            "capabilities": list(observation.capabilities),
            "runtime_healthy": observation.runtime_healthy,
            "frontier": [dict(item) for item in observation.frontier],
        },
    )
    return file_sha256(path)


def _publication_readback(
    observation: FrontierObservation, created_task_ids: Sequence[str]
) -> list[dict[str, Any]]:
    by_task = {str(item.get("task_id")): item for item in observation.frontier}
    result = []
    for task_id in created_task_ids:
        item = by_task.get(task_id)
        result.append(
            {
                "task_id": task_id,
                "present_in_frontier": item is not None,
                "effective_state": None if item is None else item.get("effective_state"),
                "queue_lane": None if item is None else item.get("queue_lane"),
                "claimable": bool(item is not None and item.get("eligible") is True),
                "reasons": [] if item is None else list(item.get("claim_reasons") or []),
            }
        )
    return result


POST_MERGE_SCHEMA_VERSION = 1
POST_MERGE_KIND = "bureau_merged_supply_state_store_reconciliation"
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupplyError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise SupplyError(f"{label} must be an object")
    return value


def _validated_supply_publication_receipt(
    path: Path, expected_sha256: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise SupplyError("run receipt SHA-256 must be an exact lowercase digest")
    if not path.is_file() or file_sha256(path) != expected_sha256:
        raise SupplyError("task-supply run receipt digest mismatch")
    receipt = _load_json_object(path, "task-supply run receipt")
    cycle = receipt.get("cycle_id")
    errors = validate_receipt(
        receipt,
        expected_stage="watchdog",
        expected_cycle_id=cycle if isinstance(cycle, str) else None,
    )
    if errors:
        raise SupplyError("task-supply run receipt contract failed: " + "; ".join(errors))
    if receipt.get("result") != "completed" or receipt.get("degraded") is not False:
        raise SupplyError("task-supply run receipt is not a successful terminal publication")
    publication_evidence = [
        item
        for item in receipt.get("evidence", [])
        if isinstance(item, Mapping) and item.get("kind") == "task_supply_publication"
    ]
    if len(publication_evidence) != 1:
        raise SupplyError("task-supply run receipt has no unique publication evidence")
    publication = publication_evidence[0].get("publication_result")
    if not isinstance(publication, dict):
        raise SupplyError("task-supply publication evidence has no result object")
    if (
        publication.get("kind") != "bureau_task_supply_publication_result"
        or publication.get("status") != "published"
        or publication.get("post_publication_registry_valid") is not True
    ):
        raise SupplyError("task-supply publication result is not terminal and valid")
    claimed_result_sha = publication.get("result_sha256")
    observed_result_sha = sha256_json(
        {key: value for key, value in publication.items() if key != "result_sha256"}
    )
    if (
        not isinstance(claimed_result_sha, str)
        or claimed_result_sha != observed_result_sha
        or publication_evidence[0].get("publication_result_sha256") != claimed_result_sha
    ):
        raise SupplyError("task-supply publication result digest mismatch")
    created = publication.get("created_tasks")
    created_ids = publication.get("created_task_ids")
    if (
        not isinstance(created, list)
        or not created
        or any(not isinstance(item, dict) for item in created)
        or not isinstance(created_ids, list)
    ):
        raise SupplyError("task-supply publication result lacks exact created TaskSpec bindings")
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in created:
        task_id = item.get("task_id")
        task_path = item.get("task_path")
        lane = item.get("queue_lane")
        file_digest = item.get("file_sha256")
        spec_digest = item.get("spec_sha256")
        if (
            not isinstance(task_id, str)
            or not task_id
            or task_id in seen
            or task_path != f"registry/tasks/{task_id}.json"
            or lane != "later"
            or not isinstance(file_digest, str)
            or SHA256_RE.fullmatch(file_digest) is None
            or not isinstance(spec_digest, str)
            or SHA256_RE.fullmatch(spec_digest) is None
        ):
            raise SupplyError("task-supply created TaskSpec binding is invalid")
        seen.add(task_id)
        bindings.append(dict(item))
    if created_ids != [item["task_id"] for item in bindings]:
        raise SupplyError("created TaskSpec id list does not match publication bindings")
    return receipt, publication, bindings


def _gh_json(arguments: Sequence[str]) -> Any:
    argv = ["gh", *arguments]
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupplyError("cannot observe Supply publication pull request") from exc
    if completed.returncode != 0:
        raise SupplyError("GitHub Supply publication observation failed")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SupplyError("GitHub Supply publication observation returned invalid JSON") from exc


def _gh_raw_content(repository: str, path: str, ref: str) -> bytes:
    encoded_path = quote(path, safe="/")
    argv = [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github.raw+json",
        f"repos/{repository}/contents/{encoded_path}?ref={ref}",
    ]
    try:
        completed = subprocess.run(argv, check=False, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupplyError(f"cannot read merged Supply file from GitHub: {path}") from exc
    if completed.returncode != 0:
        raise SupplyError(f"cannot read merged Supply file from GitHub: {path}")
    return bytes(completed.stdout)


def observe_merged_supply_pull_request(
    repository: str,
    number: int,
    bindings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if GITHUB_REPOSITORY_RE.fullmatch(repository) is None or number < 1:
        raise SupplyError("Supply publication pull request selector is invalid")
    raw = _gh_json(["api", f"repos/{repository}/pulls/{number}"])
    if not isinstance(raw, dict):
        raise SupplyError("Supply publication pull request observation is invalid")
    changed_count = raw.get("changed_files")
    if type(changed_count) is not int or changed_count < 1 or changed_count > 100:
        raise SupplyError("Supply publication pull request file count is unsupported")
    file_rows = _gh_json(["api", f"repos/{repository}/pulls/{number}/files?per_page=100"])
    if not isinstance(file_rows, list) or len(file_rows) != changed_count:
        raise SupplyError("Supply publication pull request file observation is incomplete")
    head = raw.get("head")
    base = raw.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise SupplyError("Supply publication pull request Git binding is incomplete")
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or GIT_HEAD_RE.fullmatch(head_sha) is None:
        raise SupplyError("Supply publication pull request head is invalid")
    files: dict[str, dict[str, Any]] = {}
    for row in file_rows:
        if not isinstance(row, dict) or not isinstance(row.get("filename"), str):
            raise SupplyError("Supply publication pull request file row is invalid")
        files[str(row["filename"])] = {"status": str(row.get("status") or "")}
    expected_paths = {str(item["task_path"]) for item in bindings} | {"registry/queue.json"}
    if set(files) != expected_paths:
        raise SupplyError("Supply publication pull request file set drifted")
    for binding in bindings:
        path = str(binding["task_path"])
        payload = _gh_raw_content(repository, path, head_sha)
        try:
            task = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupplyError(f"merged Supply TaskSpec is invalid JSON: {path}") from exc
        files[path].update(
            {
                "file_sha256": hashlib.sha256(payload).hexdigest(),
                "spec_sha256": task_spec_digest(task),
            }
        )
    queue_payload = _gh_raw_content(repository, "registry/queue.json", head_sha)
    files["registry/queue.json"]["file_sha256"] = hashlib.sha256(queue_payload).hexdigest()
    return {
        "number": number,
        "state": "MERGED"
        if raw.get("merged_at") and raw.get("state") == "closed"
        else str(raw.get("state") or "").upper(),
        "merged_at": raw.get("merged_at"),
        "merge_commit": raw.get("merge_commit_sha"),
        "head": head_sha,
        "branch": head.get("ref"),
        "base": base.get("ref"),
        "base_sha": base.get("sha"),
        "files": files,
    }


def _git_merge_is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupplyError("cannot verify Supply merge ancestry") from exc
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise SupplyError("cannot verify Supply merge ancestry")


def _validate_merged_supply_binding(
    *,
    registry_root: Path,
    publication: Mapping[str, Any],
    bindings: Sequence[Mapping[str, Any]],
    pull_request: Mapping[str, Any],
    head_reader: Callable[[Path], str],
    merge_ancestor_checker: Callable[[Path, str, str], bool],
) -> dict[str, Any]:
    if pull_request.get("state") != "MERGED" or not pull_request.get("merged_at"):
        raise SupplyError("Supply publication pull request is not merged")
    merge_commit = pull_request.get("merge_commit")
    pr_head = pull_request.get("head")
    if (
        not isinstance(merge_commit, str)
        or GIT_HEAD_RE.fullmatch(merge_commit) is None
        or not isinstance(pr_head, str)
        or GIT_HEAD_RE.fullmatch(pr_head) is None
        or pull_request.get("base") != "main"
        or pull_request.get("base_sha") != publication.get("registry_head_before")
    ):
        raise SupplyError("Supply publication pull request revision binding drifted")
    files = pull_request.get("files")
    if not isinstance(files, Mapping):
        raise SupplyError("Supply publication pull request files are unavailable")
    expected_paths = {str(item["task_path"]) for item in bindings} | {"registry/queue.json"}
    if set(files) != expected_paths:
        raise SupplyError("Supply publication pull request file set drifted")
    for binding in bindings:
        path = str(binding["task_path"])
        item = files.get(path)
        if (
            not isinstance(item, Mapping)
            or item.get("status") != "added"
            or item.get("file_sha256") != binding.get("file_sha256")
            or item.get("spec_sha256") != binding.get("spec_sha256")
        ):
            raise SupplyError(f"merged Supply TaskSpec binding drifted: {binding['task_id']}")
    queue_file = files.get("registry/queue.json")
    if (
        not isinstance(queue_file, Mapping)
        or queue_file.get("status") != "modified"
        or queue_file.get("file_sha256") != publication.get("queue_sha256_after")
    ):
        raise SupplyError("merged Supply queue binding drifted")

    current_head = head_reader(registry_root)
    if not merge_ancestor_checker(registry_root, merge_commit, current_head):
        raise SupplyError("current Registry does not contain the merged Supply publication")
    registry = Registry.load(registry_root)
    for binding in bindings:
        task_id = str(binding["task_id"])
        path = registry_root / str(binding["task_path"])
        task = registry.tasks.get(task_id)
        if (
            task is None
            or not path.is_file()
            or file_sha256(path) != binding.get("file_sha256")
            or task_spec_digest(task.raw) != binding.get("spec_sha256")
        ):
            raise SupplyError(f"current Registry Supply TaskSpec drifted: {task_id}")
    queue_path = registry_root / "registry/queue.json"
    queue = _load_json_object(queue_path, "current Registry queue")
    lanes = queue.get("lanes")
    if not isinstance(lanes, dict):
        raise SupplyError("current Registry queue has no lanes")
    for binding in bindings:
        task_id = str(binding["task_id"])
        expected_lane = str(binding["queue_lane"])
        occurrences = [
            (str(lane), values.count(task_id))
            for lane, values in lanes.items()
            if isinstance(values, list) and task_id in values
        ]
        if occurrences != [(expected_lane, 1)]:
            raise SupplyError(f"current Registry queue membership drifted: {task_id}")
    return {
        "head": current_head,
        "merge_commit": merge_commit,
        "pull_request_head": pr_head,
        "queue_sha256": file_sha256(queue_path),
        "registry": registry,
    }


def _state_binding_readback(
    store: ReadOnlyStateStore | StateStore,
    bindings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for binding in bindings:
        task_id = str(binding["task_id"])
        expected_digest = str(binding["spec_sha256"])
        current = store.task_spec(task_id)
        if current is None:
            status = "missing"
            current_view = None
        else:
            status = (
                "expected-present"
                if current["spec_sha256"] == expected_digest
                else "divergent-present"
            )
            current_view = {
                "revision": current["revision"],
                "spec_sha256": current["spec_sha256"],
            }
        result.append(
            {
                "task_id": task_id,
                "expected_spec_sha256": expected_digest,
                "status": status,
                "current": current_view,
            }
        )
    return result


def _create_only_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SupplyError("post-merge reconciliation receipt already exists") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short post-merge reconciliation receipt write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _validated_reconciliation_receipt(
    *,
    path: Path,
    expected_run_receipt_sha256: str,
    publication_result_sha256: str,
    repository: str,
    pull_request_number: int,
    store: ReadOnlyStateStore,
) -> dict[str, Any]:
    receipt = _load_json_object(path, "post-merge reconciliation receipt")
    if (
        receipt.get("schema_version") != POST_MERGE_SCHEMA_VERSION
        or receipt.get("kind") != POST_MERGE_KIND
    ):
        raise SupplyError("post-merge reconciliation receipt contract is invalid")
    claimed = receipt.get("receipt_sha256")
    observed = sha256_json(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if not isinstance(claimed, str) or claimed != observed:
        raise SupplyError("post-merge reconciliation receipt digest mismatch")
    if (
        receipt.get("run_receipt_sha256") != expected_run_receipt_sha256
        or receipt.get("publication_result_sha256") != publication_result_sha256
        or receipt.get("repository") != repository
        or receipt.get("pull_request") != pull_request_number
    ):
        raise SupplyError("post-merge reconciliation receipt binding drifted")
    state_results = receipt.get("state_results")
    if not isinstance(state_results, list):
        raise SupplyError("post-merge reconciliation receipt has no StateStore readback")
    for item in state_results:
        if not isinstance(item, Mapping) or not isinstance(item.get("task_id"), str):
            raise SupplyError("post-merge reconciliation receipt StateStore row is invalid")
        after = item.get("after")
        current = store.task_spec(str(item["task_id"]))
        if (
            not isinstance(after, Mapping)
            or current is None
            or current.get("revision") != after.get("revision")
            or current.get("spec_sha256") != after.get("spec_sha256")
        ):
            raise SupplyError("post-merge reconciliation StateStore readback drifted")
    return receipt


def reconcile_merged_supply_publication(
    *,
    registry_root: Path,
    run_receipt_path: Path,
    expected_run_receipt_sha256: str,
    repository: str,
    pull_request_number: int,
    capabilities: Sequence[str],
    state_db: Path | None = None,
    state_store_root: Path | None = None,
    mode: str = "preview",
    reconciliation_receipt_path: Path | None = None,
    pull_request_reader: Callable[
        [str, int, Sequence[Mapping[str, Any]]], Mapping[str, Any]
    ] = observe_merged_supply_pull_request,
    head_reader: Callable[[Path], str] = _git_head,
    merge_ancestor_checker: Callable[[Path, str, str], bool] = _git_merge_is_ancestor,
    observer: Callable[..., FrontierObservation] = observe_authoritative_frontier,
) -> dict[str, Any]:
    if mode not in {"preview", "apply", "readback"}:
        raise SupplyError("unsupported post-merge reconciliation mode")
    if state_db is None and state_store_root is None:
        raise SupplyError("post-merge reconciliation requires an explicit Bureau StateStore path")
    selected_root = registry_root.expanduser().resolve()
    selected_run_receipt = run_receipt_path.expanduser().resolve()
    run_receipt, publication, bindings = _validated_supply_publication_receipt(
        selected_run_receipt, expected_run_receipt_sha256
    )
    observed_pr = pull_request_reader(repository, pull_request_number, bindings)
    if observed_pr.get("number") != pull_request_number:
        raise SupplyError("Supply publication pull request number drifted")
    registry_binding = _validate_merged_supply_binding(
        registry_root=selected_root,
        publication=publication,
        bindings=bindings,
        pull_request=observed_pr,
        head_reader=head_reader,
        merge_ancestor_checker=merge_ancestor_checker,
    )
    read_store = ReadOnlyStateStore(state_db, state_store_root)
    state_before = _state_binding_readback(read_store, bindings)
    receipt_path = (
        reconciliation_receipt_path.expanduser().resolve()
        if reconciliation_receipt_path is not None
        else None
    )
    publication_result_sha256 = str(publication["result_sha256"])

    if receipt_path is not None and receipt_path.exists():
        receipt = _validated_reconciliation_receipt(
            path=receipt_path,
            expected_run_receipt_sha256=expected_run_receipt_sha256,
            publication_result_sha256=publication_result_sha256,
            repository=repository,
            pull_request_number=pull_request_number,
            store=read_store,
        )
        return {
            **receipt,
            "receipt_path": str(receipt_path),
            "receipt_file_sha256": file_sha256(receipt_path),
            "idempotent_replay": True,
            "effect_started": False,
        }
    if mode == "readback":
        return {
            "schema_version": POST_MERGE_SCHEMA_VERSION,
            "kind": POST_MERGE_KIND,
            "status": "receipt-missing",
            "effect_started": False,
            "idempotent_replay": False,
            "run_receipt_sha256": expected_run_receipt_sha256,
            "publication_result_sha256": publication_result_sha256,
            "repository": repository,
            "pull_request": pull_request_number,
            "state_readback": state_before,
            "does_not_establish": [
                "a completed post-merge effect",
                "permission to retry without review",
            ],
        }
    if mode == "preview":
        return {
            "schema_version": POST_MERGE_SCHEMA_VERSION,
            "kind": POST_MERGE_KIND,
            "status": "ready",
            "effect_started": False,
            "read_only": True,
            "run_receipt_sha256": expected_run_receipt_sha256,
            "publication_result_sha256": publication_result_sha256,
            "repository": repository,
            "pull_request": pull_request_number,
            "merge_commit": registry_binding["merge_commit"],
            "registry_head": registry_binding["head"],
            "state_preimage": state_before,
            "does_not_establish": ["StateStore materialization", "claimability", "queue mutation"],
        }
    if receipt_path is None:
        raise SupplyError("post-merge apply requires a create-only reconciliation receipt path")

    rechecked_run_receipt, rechecked_publication, rechecked_bindings = (
        _validated_supply_publication_receipt(
            selected_run_receipt, expected_run_receipt_sha256
        )
    )
    if (
        rechecked_run_receipt != run_receipt
        or rechecked_publication != publication
        or rechecked_bindings != bindings
    ):
        raise SupplyError("task-supply publication receipt changed before StateStore effect")
    rechecked_pr = pull_request_reader(repository, pull_request_number, rechecked_bindings)
    if rechecked_pr.get("number") != pull_request_number:
        raise SupplyError("Supply publication pull request number drifted before StateStore effect")
    registry_binding = _validate_merged_supply_binding(
        registry_root=selected_root,
        publication=rechecked_publication,
        bindings=rechecked_bindings,
        pull_request=rechecked_pr,
        head_reader=head_reader,
        merge_ancestor_checker=merge_ancestor_checker,
    )
    run_receipt = rechecked_run_receipt
    publication = rechecked_publication
    bindings = rechecked_bindings
    observed_pr = rechecked_pr

    write_store = StateStore(state_db, state_store_root)
    state_results: list[dict[str, Any]] = []
    try:
        for binding, _before in zip(bindings, state_before, strict=True):
            task_id = str(binding["task_id"])
            expected_digest = str(binding["spec_sha256"])
            current = write_store.task_spec(task_id)
            before_view = (
                None
                if current is None
                else {
                    "revision": current["revision"],
                    "spec_sha256": current["spec_sha256"],
                }
            )
            if current is None:
                seeded = write_store.seed_missing_registry_task_spec(
                    registry_binding["registry"], task_id
                )
                status = "imported" if seeded.get("changed") else "unchanged"
            elif current["spec_sha256"] == expected_digest:
                status = "unchanged"
            else:
                status = "divergent-preserved"
            after = write_store.task_spec(task_id)
            if after is None:
                raise SupplyError(f"post-merge StateStore readback missing: {task_id}")
            if status != "divergent-preserved" and after["spec_sha256"] != expected_digest:
                raise SupplyError(f"post-merge StateStore digest mismatch: {task_id}")
            if status == "divergent-preserved" and (
                before_view is None
                or after["revision"] != before_view["revision"]
                or after["spec_sha256"] != before_view["spec_sha256"]
            ):
                raise SupplyError(f"divergent StateStore TaskSpec changed unexpectedly: {task_id}")
            state_results.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "expected_spec_sha256": expected_digest,
                    "before": before_view,
                    "after": {
                        "revision": after["revision"],
                        "spec_sha256": after["spec_sha256"],
                    },
                }
            )
    except Exception as exc:
        raise SupplyError(
            "post-merge StateStore effect may be partial; run exact readback before any retry"
        ) from exc

    try:
        dispatch_observation = observer(
            registry_root=selected_root,
            capabilities=capabilities,
            state_db=state_db,
            state_store_root=state_store_root,
        )
        dispatcher_readback = _publication_readback(
            dispatch_observation, [str(item["task_id"]) for item in bindings]
        )
        status = (
            "completed-with-divergence"
            if any(item["status"] == "divergent-preserved" for item in state_results)
            else "completed"
        )
        receipt = {
            "schema_version": POST_MERGE_SCHEMA_VERSION,
            "kind": POST_MERGE_KIND,
            "created_at": utc_now(),
            "status": status,
            "effect_started": any(item["status"] == "imported" for item in state_results),
            "idempotent_replay": False,
            "run_receipt_path": str(selected_run_receipt),
            "run_receipt_sha256": expected_run_receipt_sha256,
            "run_id": run_receipt.get("run_id"),
            "publication_result_sha256": publication_result_sha256,
            "repository": repository,
            "pull_request": pull_request_number,
            "pull_request_head": observed_pr.get("head"),
            "merge_commit": registry_binding["merge_commit"],
            "registry_head": registry_binding["head"],
            "registry_queue_sha256": registry_binding["queue_sha256"],
            "state_results": state_results,
            "dispatcher_readback": dispatcher_readback,
            "does_not_establish": [
                "claimability beyond the normal Dispatcher gates",
                "queue mutation",
                "merge or deployment authority",
            ],
        }
        receipt["receipt_sha256"] = sha256_json(receipt)
        _create_only_json(receipt_path, receipt)
    except Exception as exc:
        raise SupplyError(
            "post-merge StateStore effect may already be complete while Dispatcher/receipt "
            "readback is incomplete; run exact readback before any retry"
        ) from exc
    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "receipt_file_sha256": file_sha256(receipt_path),
    }


def run_supply_cycle(
    *,
    registry_root: Path,
    capabilities: Sequence[str],
    state_root: Path | None = None,
    state_db: Path | None = None,
    state_store_root: Path | None = None,
    policy: SupplyPolicy | None = None,
    approval_available: bool = False,
    mutation_authority: bool = False,
    publish: bool = False,
    environment_blockers: Sequence[str] = (),
    generated_at: str | None = None,
    registry_head: str | None = None,
    acceptance_contracts: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    observer: Callable[..., FrontierObservation] = observe_authoritative_frontier,
    head_reader: Callable[[Path], str] = _git_head,
) -> dict[str, Any]:
    """Observe, report, and — only under explicit authority — publish bounded refill."""
    selected_root = (state_root or default_state_root()).expanduser()
    resolved_registry_root = registry_root.expanduser().resolve()
    now = generated_at or utc_now()
    selected_head_reader = head_reader
    if registry_head is not None:
        if not isinstance(registry_head, str) or GIT_HEAD_RE.fullmatch(registry_head) is None:
            raise SupplyError("registry_head must be an exact lowercase 40-character Git commit")
        if publish:
            raise SupplyError(
                "manifest-bound registry_head is read-only; publication requires "
                "a Git-bound Registry"
            )
        head = registry_head

        def selected_head_reader(_root: Path) -> str:
            return head
    else:
        head = head_reader(resolved_registry_root)
    queue_digest = file_sha256(resolved_registry_root / "registry/queue.json")
    observation = observer(
        registry_root=resolved_registry_root,
        capabilities=capabilities,
        state_db=state_db,
        state_store_root=state_store_root,
    )
    snapshot_file = snapshot_path(selected_root)
    snapshot_digest = _write_snapshot(
        snapshot_file,
        observation,
        registry_root=resolved_registry_root,
        registry_head=head,
        queue_sha256=queue_digest,
        generated_at=now,
    )
    blockers = list(environment_blockers)
    blockers.extend(f"runtime-blocker:{code}" for code in observation.runtime_blocker_codes)
    report = build_registry_supply_report(
        registry_root=resolved_registry_root,
        frontier=list(observation.frontier),
        policy=policy,
        approval_available=approval_available,
        runtime_healthy=observation.runtime_healthy,
        mutation_authority=mutation_authority,
        environment_blockers=tuple(blockers),
        frontier_registry_head=head,
        frontier_queue_sha256=queue_digest,
        frontier_snapshot_sha256=snapshot_digest,
        acceptance_contracts=acceptance_contracts,
        head_reader=selected_head_reader,
    )
    atomic_json(report_path(selected_root), report)

    plan = report["publication_plan"]
    publication: dict[str, Any] = {
        "attempted": False,
        "status": "preview-only",
        "created_task_ids": [],
        "post_publication_readback": [],
    }
    if not publish:
        publication["reason"] = "publication was not requested"
    elif not mutation_authority:
        publication["reason"] = "registry mutation authority is not granted"
    elif plan.get("status") != "authorized":
        publication["reason"] = "publication plan is not authorized and blocker-free"
    else:
        publication["attempted"] = True
        try:
            result = publish_supply_plan(
                plan,
                mutation_authorized=True,
                expected_plan_sha256=str(plan["plan_sha256"]),
                head_reader=selected_head_reader,
            )
        except SupplyError as exc:
            publication["status"] = "failed"
            publication["reason"] = str(exc)
        else:
            publication["status"] = "published"
            publication["reason"] = "bounded canonical refill published under explicit authority"
            publication["result"] = result
            publication["created_task_ids"] = list(result["created_task_ids"])
            publication["post_publication_readback"] = _publication_readback(
                observer(
                    registry_root=resolved_registry_root,
                    capabilities=capabilities,
                    state_db=state_db,
                    state_store_root=state_store_root,
                ),
                result["created_task_ids"],
            )
    return {
        "schema_version": CYCLE_SCHEMA_VERSION,
        "kind": CYCLE_KIND,
        "generated_at": now,
        "status": report["status"],
        "registry": report["registry"],
        "capabilities": list(observation.capabilities),
        "runtime_healthy": observation.runtime_healthy,
        "mutation_authority_observed": mutation_authority,
        "metrics": report["metrics"],
        "blockers": report["blockers"],
        "publication": publication,
        "report_path": str(report_path(selected_root)),
        "report_sha256": report["report_sha256"],
        "snapshot_path": str(snapshot_file),
        "snapshot_sha256": snapshot_digest,
        "does_not_establish": [
            "claimability of published work before the normal claim gates run again",
            "permission to bypass leases, capabilities, runtime health, or open-PR guards",
            "merge or deployment authority",
        ],
    }


def _cycle_result(summary: dict[str, Any]) -> str:
    if summary["status"] == "blocked":
        return "blocked"
    if summary["publication"]["status"] == "failed":
        return "failed"
    if summary["publication"]["status"] == "published":
        return "completed"
    if summary["status"] == "satisfied":
        return "idle"
    return "partial"


def _next_action(summary: dict[str, Any]) -> str:
    if summary["status"] == "satisfied":
        return "claimable supply is at or above its floor; dispatch normal work"
    if summary["status"] == "blocked":
        return "resolve the exact supply blockers before requesting mutation authority"
    if summary["publication"]["status"] == "published":
        return "re-run the normal claim path against the published canonical tasks"
    return "review the bounded publication plan and grant mutation authority for it"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bureau-task-supply-runner")
    parser.add_argument("--registry-root", default=".")
    parser.add_argument("--capability", action="append", default=[], required=True)
    parser.add_argument("--state-root", help="task-supply artifact root")
    parser.add_argument("--bureau-state-db")
    parser.add_argument("--bureau-state-root")
    parser.add_argument("--floor", type=int, default=8)
    parser.add_argument("--refill-target", type=int, default=12)
    parser.add_argument("--max-new-per-cycle", type=int, default=4)
    parser.add_argument("--bucket-hours", type=int, default=24)
    parser.add_argument("--approval-available", action="store_true")
    parser.add_argument("--mutation-authority", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--environment-blocker", action="append", default=[])
    parser.add_argument("--reconcile-merged-run")
    parser.add_argument("--expected-run-receipt-sha256")
    parser.add_argument("--publication-repository")
    parser.add_argument("--publication-pr", type=int)
    parser.add_argument("--reconciliation-receipt")
    reconcile_mode = parser.add_mutually_exclusive_group()
    reconcile_mode.add_argument("--reconcile-preview", action="store_true")
    reconcile_mode.add_argument("--reconcile-apply", action="store_true")
    reconcile_mode.add_argument("--reconcile-readback", action="store_true")
    args = parser.parse_args(argv)
    reconcile_flags = (
        args.reconcile_preview,
        args.reconcile_apply,
        args.reconcile_readback,
    )
    if args.reconcile_merged_run:
        if sum(bool(value) for value in reconcile_flags) != 1:
            parser.error("post-merge reconciliation requires exactly one mode")
        if (
            not args.expected_run_receipt_sha256
            or not args.publication_repository
            or not args.publication_pr
            or (not args.bureau_state_db and not args.bureau_state_root)
        ):
            parser.error(
                "post-merge reconciliation requires receipt digest, repository, PR, "
                "and explicit Bureau StateStore"
            )
        if args.reconcile_apply and not args.reconciliation_receipt:
            parser.error("post-merge apply requires --reconciliation-receipt")
        selected_mode = (
            "apply"
            if args.reconcile_apply
            else "readback"
            if args.reconcile_readback
            else "preview"
        )
        try:
            reconciled = reconcile_merged_supply_publication(
                registry_root=Path(args.registry_root),
                run_receipt_path=Path(args.reconcile_merged_run),
                expected_run_receipt_sha256=args.expected_run_receipt_sha256,
                repository=args.publication_repository,
                pull_request_number=args.publication_pr,
                capabilities=args.capability,
                state_db=Path(args.bureau_state_db) if args.bureau_state_db else None,
                state_store_root=(Path(args.bureau_state_root) if args.bureau_state_root else None),
                mode=selected_mode,
                reconciliation_receipt_path=(
                    Path(args.reconciliation_receipt) if args.reconciliation_receipt else None
                ),
            )
        except SupplyError as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
            return 1
        print(json.dumps(reconciled, ensure_ascii=False, sort_keys=True))
        return 0
    if any(reconcile_flags) or any(
        value is not None
        for value in (
            args.expected_run_receipt_sha256,
            args.publication_repository,
            args.publication_pr,
            args.reconciliation_receipt,
        )
    ):
        parser.error("post-merge reconciliation arguments require --reconcile-merged-run")
    selected_root = Path(args.state_root).expanduser() if args.state_root else default_state_root()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    selected_cycle = cycle_id()
    started_at = utc_now()
    degraded = False
    evidence: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    try:
        summary = run_supply_cycle(
            registry_root=Path(args.registry_root).expanduser(),
            capabilities=args.capability,
            state_root=selected_root,
            state_db=Path(args.bureau_state_db).expanduser() if args.bureau_state_db else None,
            state_store_root=(
                Path(args.bureau_state_root).expanduser() if args.bureau_state_root else None
            ),
            policy=SupplyPolicy(
                floor=args.floor,
                refill_target=args.refill_target,
                max_new_per_cycle=args.max_new_per_cycle,
                bucket_hours=args.bucket_hours,
            ),
            approval_available=args.approval_available,
            mutation_authority=args.mutation_authority,
            publish=args.publish,
            environment_blockers=tuple(args.environment_blocker),
        )
        result = _cycle_result(summary)
        degraded = result in {"blocked", "failed"}
        evidence.append(
            {
                "kind": "task_supply_report",
                "path": summary["report_path"],
                "report_sha256": summary["report_sha256"],
                "status": summary["status"],
                "metrics": summary["metrics"],
                "blockers": summary["blockers"],
                "publication_status": summary["publication"]["status"],
                "created_task_ids": summary["publication"]["created_task_ids"],
            }
        )
        publication_result = summary["publication"].get("result")
        if isinstance(publication_result, dict):
            evidence.append(
                {
                    "kind": "task_supply_publication",
                    "publication_result_sha256": publication_result.get("result_sha256"),
                    "publication_result": publication_result,
                }
            )
        next_action = _next_action(summary)
    except Exception as exc:  # receipt first, crash never silent
        degraded = True
        result = "failed"
        evidence.append({"kind": "task_supply_error", "error": str(exc)[:2000]})
        next_action = "repair the task-supply stage before trusting claimable-supply counts"
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": selected_cycle,
        "stage": "watchdog",
        "run_id": f"task-supply-{stamp}",
        "trigger": "local-task-supply",
        "started_at": started_at,
        "finished_at": utc_now(),
        "lifecycle_state": "terminal",
        "result": result,
        "degraded": degraded,
        "evidence": evidence,
        "next_action": next_action,
        "receipt_path": str(selected_root / "runs" / f"{stamp}-task-supply.json"),
    }
    errors = validate_receipt(receipt, expected_stage="watchdog", expected_cycle_id=selected_cycle)
    if errors:
        raise RuntimeError("task supply receipt contract failed: " + "; ".join(errors))
    atomic_json(Path(receipt["receipt_path"]), receipt)
    atomic_json(selected_root / "latest.json", receipt)
    print(
        json.dumps(
            {
                "status": result,
                "supply_status": None if summary is None else summary["status"],
                "degraded": degraded,
                "report": None if summary is None else summary["report_path"],
                "receipt": receipt["receipt_path"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if result == "failed":
        return 1
    return 2 if result == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
