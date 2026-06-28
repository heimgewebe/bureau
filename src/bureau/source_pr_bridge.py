from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Sequence
from typing import Any

DEFAULT_REPOSITORY = "heimgewebe/bureau"
DEFAULT_BASE = "main"
DEFAULT_BRANCH = "automation/weltgewebe-source-sync"


class GhCommandError(RuntimeError):
    """Raised when the GitHub CLI cannot complete a bridge operation."""


def _run(arguments: Sequence[str], *, allow_not_found: bool = False) -> str | None:
    process = subprocess.run(
        ["gh", *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if process.returncode == 0:
        return process.stdout.strip()

    detail = "\n".join(part for part in (process.stdout.strip(), process.stderr.strip()) if part)
    if allow_not_found and ("HTTP 404" in detail or '"status":"404"' in detail):
        return None
    raise GhCommandError(f"gh {' '.join(arguments)} failed: {detail}")


def _json(arguments: Sequence[str], *, allow_not_found: bool = False) -> Any:
    output = _run(arguments, allow_not_found=allow_not_found)
    if output is None:
        return None
    return json.loads(output)


def _pull_request_body(branch: str, head_sha: str) -> str:
    return (
        "## Automated source observation\n\n"
        "Bureau observed a changed, commit-bound Weltgewebe task snapshot.\n\n"
        f"- snapshot branch: `{branch}`\n"
        f"- snapshot commit: `{head_sha}`\n"
        "- generated path: `registry/sources/weltgewebe.json`\n\n"
        "This proposal updates observation data only. It does not materialize executable "
        "Bureau tasks, establish readiness, infer dependencies or resource claims, or grant "
        "autonomous execution.\n"
    )


def reconcile(
    repository: str = DEFAULT_REPOSITORY,
    base: str = DEFAULT_BASE,
    branch: str = DEFAULT_BRANCH,
) -> dict[str, Any]:
    ref = _json(
        ["api", f"repos/{repository}/git/ref/heads/{branch}"],
        allow_not_found=True,
    )
    if ref is None:
        return {"status": "branch_absent", "repository": repository, "branch": branch}

    head_sha = str(ref["object"]["sha"])
    comparison = _json(["api", f"repos/{repository}/compare/{base}...{branch}"])
    ahead_by = int(comparison.get("ahead_by", 0))
    if ahead_by <= 0:
        return {
            "status": "no_change",
            "repository": repository,
            "base": base,
            "branch": branch,
            "head_sha": head_sha,
            "ahead_by": ahead_by,
        }

    pull_requests = _json(
        [
            "pr",
            "list",
            "--repo",
            repository,
            "--base",
            base,
            "--head",
            branch,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "number,url",
        ]
    )
    body = _pull_request_body(branch, head_sha)
    if pull_requests:
        number = str(pull_requests[0]["number"])
        _run(["pr", "edit", number, "--repo", repository, "--body", body])
        return {
            "status": "updated",
            "repository": repository,
            "base": base,
            "branch": branch,
            "head_sha": head_sha,
            "ahead_by": ahead_by,
            "pull_request": int(number),
            "url": pull_requests[0]["url"],
        }

    url = _run(
        [
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            "chore: sync Weltgewebe source snapshot",
            "--body",
            body,
        ]
    )
    return {
        "status": "created",
        "repository": repository,
        "base": base,
        "branch": branch,
        "head_sha": head_sha,
        "ahead_by": ahead_by,
        "url": url,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update the review PR for the Weltgewebe source snapshot branch."
    )
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = reconcile(arguments.repo, arguments.base, arguments.branch)
    except (GhCommandError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"source PR bridge failed: {error}") from error
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
