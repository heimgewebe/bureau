#!/usr/bin/env python3
"""Classify a Git diff into one safe validation scope.

Only two narrow Registry changes may bypass the full code suite:

* task-only: one or more added/modified canonical task documents;
* queue-only: exactly one modification of ``registry/queue.json``.

Every malformed, mixed, renamed, copied, deleted or unknown change falls back to
``full``. A failed Git comparison also falls back to ``full``.
"""

from __future__ import annotations

import argparse
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
    return classify_entries(parse_name_status(completed.stdout))


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
