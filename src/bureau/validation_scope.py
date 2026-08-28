#!/usr/bin/env python3
"""Classify a Git diff into one safe validation scope.

Only two narrow Registry changes may bypass the full code suite:

* task-only: added/modified canonical task documents that do not touch a
  runtime-refresh source-precondition authority;
* queue-only: exactly one modification of ``registry/queue.json``.

Every malformed, mixed, renamed, copied, deleted or unknown change falls back to
``full``. A failed Git comparison also falls back to ``full``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence

FULL = "full"
TASK_ONLY = "task-only"
QUEUE_ONLY = "queue-only"
VALID_SCOPES = frozenset({FULL, TASK_ONLY, QUEUE_ONLY})
TASK_PATH_RE = re.compile(r"registry/tasks/[A-Za-z0-9][A-Za-z0-9._:-]{0,239}\.json")
SHA_RE = re.compile(r"[0-9a-f]{40,64}")

Entry = tuple[str, tuple[str, ...]]


def parse_name_status(text: str) -> list[Entry]:
    """Parse ``git diff --name-status`` output without normalizing ambiguity."""

    entries: list[Entry] = []
    for raw_line in text.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split("\t")
        if len(fields) < 2 or not fields[0]:
            return [("INVALID", ())]
        entries.append((fields[0], tuple(fields[1:])))
    return entries


def classify_entries(entries: Sequence[Entry]) -> str:
    """Return the narrowest safe scope for already parsed entries."""

    if not entries:
        return FULL

    task_only = all(
        status in {"A", "M"}
        and len(paths) == 1
        and TASK_PATH_RE.fullmatch(paths[0]) is not None
        for status, paths in entries
    )
    if task_only:
        return TASK_ONLY

    if list(entries) == [("M", ("registry/queue.json",))]:
        return QUEUE_ONLY

    return FULL


def _task_spec_at_revision(revision: str, path: str) -> dict[str, object] | None:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _has_source_precondition_authority(spec: dict[str, object]) -> bool:
    metadata = spec.get("metadata")
    authority = (
        metadata.get("runtime_refresh_authority")
        if isinstance(metadata, dict)
        else None
    )
    return isinstance(authority, dict) and "source_precondition" in authority


def task_changes_require_full_validation(
    entries: Sequence[Entry], base_sha: str, head_sha: str
) -> bool:
    """Fail closed when task-only changes touch the runtime-refresh authority ratchet."""

    for status, paths in entries:
        if status not in {"A", "M"} or len(paths) != 1:
            return True
        path = paths[0]
        revisions = (head_sha,) if status == "A" else (base_sha, head_sha)
        for revision in revisions:
            spec = _task_spec_at_revision(revision, path)
            if spec is None or _has_source_precondition_authority(spec):
                return True
    return False


def classify_git_diff(base_sha: str, head_sha: str) -> str:
    """Classify one three-dot Git comparison, failing closed to ``full``."""

    if SHA_RE.fullmatch(base_sha) is None or SHA_RE.fullmatch(head_sha) is None:
        return FULL

    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            f"{base_sha}...{head_sha}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or "git diff failed"
        print(f"validation-scope: {diagnostic}", file=sys.stderr)
        return FULL
    entries = parse_name_status(completed.stdout)
    scope = classify_entries(entries)
    if scope == TASK_ONLY and task_changes_require_full_validation(
        entries, base_sha, head_sha
    ):
        return FULL
    return scope


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-sha", required=True)
    result.add_argument("--head-sha", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    scope = classify_git_diff(args.base_sha, args.head_sha)
    if scope not in VALID_SCOPES:
        scope = FULL
    print(scope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
