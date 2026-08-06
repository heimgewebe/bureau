from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import now_refill
from .core import StateStore
from .now_refill import NowRefillPolicy
from .v2 import Registry

DEFAULT_REPOSITORY = "heimgewebe/bureau"
DEFAULT_BASE = "main"
DEFAULT_BRANCH = "automation/weltgewebe-source-sync"
NOW_REFILL_BRANCH = "automation/now-lane-refill"
NOW_REFILL_COMMIT_MESSAGE = "chore(queue): refill Bureau Now lane"
NOW_REFILL_GIT_AUTHOR_NAME = "bureau-source-pr-bridge"
NOW_REFILL_GIT_AUTHOR_EMAIL = "bureau-source-pr-bridge@users.noreply.github.com"


class GhCommandError(RuntimeError):
    """Raised when the GitHub CLI cannot complete a bridge operation."""


class GitCommandError(RuntimeError):
    """Raised when local Git publication of a Now-refill proposal fails."""


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


def _git(repo: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if process.returncode != 0:
        detail = "\n".join(
            part for part in (process.stdout.strip(), process.stderr.strip()) if part
        )
        raise GitCommandError(f"git {' '.join(arguments)} failed: {detail}")
    return process.stdout.strip()


def publish_now_refill(
    root: Path,
    *,
    state_db: Path | None = None,
    state_root: Path | None = None,
    policy: NowRefillPolicy | None = None,
    authority: str,
    repository: str = DEFAULT_REPOSITORY,
    base: str = DEFAULT_BASE,
    branch: str = NOW_REFILL_BRANCH,
) -> dict[str, Any]:
    """Compute and publish one bounded Now-refill proposal from real local truth.

    The decision runs against a throwaway worktree pinned to the canonical
    ``origin/<base>`` tip, so the operator's own checkout at ``root`` is never
    switched, staged or committed to. The refill decision itself is computed with
    the real local Bureau StateStore, active runs, open-PR observation and runtime
    gate bound into ``bureau.now_refill`` -- the same live truth the claim path
    uses. A GitHub-hosted runner never sees this state and therefore never decides
    or invents a promotion; it may at most transport the exact, unmodified,
    revision-bound branch this function publishes.

    Returns a ``not-applied`` result without any Git side effect when the refill
    decision does not require or cannot authorise a promotion (already satisfied,
    no structurally runnable candidate, or a blocked runtime/registry gate).
    """
    _git(root, "fetch", "origin", base)
    remote_head = _git(root, "rev-parse", f"origin/{base}")
    with tempfile.TemporaryDirectory(prefix="bureau-now-refill-") as raw_tmp:
        worktree = Path(raw_tmp) / "checkout"
        _git(root, "worktree", "add", "--detach", str(worktree), remote_head)
        try:
            registry = Registry.load(worktree)
            store = StateStore(state_db, state_root)
            result = now_refill.apply_now_refill(
                registry, store, authority=authority, policy=policy
            )
            if not result["applied"]:
                return {
                    "status": "not-applied",
                    "refill_status": result["status"],
                    "blockers": result["blockers"],
                    "report_sha256": result["report_sha256"],
                }
            _git(worktree, "add", "registry/queue.json")
            _git(
                worktree,
                "-c",
                f"user.name={NOW_REFILL_GIT_AUTHOR_NAME}",
                "-c",
                f"user.email={NOW_REFILL_GIT_AUTHOR_EMAIL}",
                "commit",
                "-m",
                NOW_REFILL_COMMIT_MESSAGE,
            )
            head_sha = _git(worktree, "rev-parse", "HEAD")
            existing_ref = _json(
                ["api", f"repos/{repository}/git/ref/heads/{branch}"],
                allow_not_found=True,
            )
            if existing_ref is None:
                _git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
            else:
                expected_remote = str(existing_ref["object"]["sha"])
                _git(
                    worktree,
                    "push",
                    f"--force-with-lease=refs/heads/{branch}:{expected_remote}",
                    "origin",
                    f"HEAD:refs/heads/{branch}",
                )
            return {
                "status": "published",
                "branch": branch,
                "base": base,
                "base_sha": remote_head,
                "head_sha": head_sha,
                "queue_sha256_before": result["registry"]["queue_sha256_before"],
                "queue_sha256_after": result["queue_sha256_after"],
                "report_sha256": result["report_sha256"],
                "promotions": result["promotions"],
            }
        finally:
            _git(root, "worktree", "remove", "--force", str(worktree))


def _pull_request_title(kind: str) -> str:
    if kind == "now-refill":
        return "chore(queue): refill Bureau Now lane"
    return "chore: sync Weltgewebe source snapshot"


def _pull_request_body(branch: str, head_sha: str, *, kind: str) -> str:
    if kind == "now-refill":
        return (
            "## Automated Now-lane refill\n\n"
            "Bureau's local, authenticated bridge computed this promotion from real "
            "local truth (Registry head, queue digest, StateStore, active runs, "
            "open-PR bindings and the runtime gate) and promoted existing "
            "structurally runnable tasks from Next to Now. No GitHub-hosted runner "
            "decided or invented this proposal; it may at most transport this exact, "
            "unmodified, revision-bound branch.\n\n"
            "Bureau-Task: OPERATOR-INTEGRATION-LOOP-V1-T029\n\n"
            f"- proposal branch: `{branch}`\n"
            f"- proposal commit: `{head_sha}`\n"
            "- bounded path: `registry/queue.json`\n\n"
            "This proposal changes prioritisation only. It does not claim or start tasks. "
            "Pickup-time approval, capability, lease, open-PR and runtime gates remain "
            "authoritative.\n"
        )
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


def _enable_auto_merge(repository: str, pull_request: int) -> None:
    _run(
        [
            "pr",
            "merge",
            str(pull_request),
            "--repo",
            repository,
            "--auto",
            "--squash",
        ]
    )


def reconcile(
    repository: str = DEFAULT_REPOSITORY,
    base: str = DEFAULT_BASE,
    branch: str = DEFAULT_BRANCH,
    *,
    kind: str = "source",
    auto_merge: bool = False,
) -> dict[str, Any]:
    if kind not in {"source", "now-refill"}:
        raise ValueError(f"unsupported bridge kind: {kind}")
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
    body = _pull_request_body(branch, head_sha, kind=kind)
    if pull_requests:
        number = int(pull_requests[0]["number"])
        _run(["pr", "edit", str(number), "--repo", repository, "--body", body])
        if auto_merge:
            _enable_auto_merge(repository, number)
        return {
            "status": "updated",
            "repository": repository,
            "base": base,
            "branch": branch,
            "head_sha": head_sha,
            "ahead_by": ahead_by,
            "pull_request": number,
            "url": pull_requests[0]["url"],
            "auto_merge_requested": auto_merge,
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
            _pull_request_title(kind),
            "--body",
            body,
        ]
    )
    if url is None:
        raise GhCommandError("gh pr create returned no pull-request URL")
    pull_request = int(url.rstrip("/").rsplit("/", 1)[-1])
    if auto_merge:
        _enable_auto_merge(repository, pull_request)
    return {
        "status": "created",
        "repository": repository,
        "base": base,
        "branch": branch,
        "head_sha": head_sha,
        "ahead_by": ahead_by,
        "pull_request": pull_request,
        "url": url,
        "auto_merge_requested": auto_merge,
    }


def withdraw_open_proposal(
    repository: str = DEFAULT_REPOSITORY,
    base: str = DEFAULT_BASE,
    branch: str = NOW_REFILL_BRANCH,
) -> dict[str, Any]:
    """Close one stale Now-refill PR when local truth authorises no proposal."""
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
    if not pull_requests:
        return {
            "status": "no_open_proposal",
            "repository": repository,
            "base": base,
            "branch": branch,
        }
    number = int(pull_requests[0]["number"])
    _run(
        [
            "pr",
            "close",
            str(number),
            "--repo",
            repository,
            "--comment",
            (
                "The current local authoritative Now-refill decision does not authorise "
                "a queue proposal. Closing this stale proposal fail-closed; a later "
                "authorised cycle may publish a fresh revision-bound branch."
            ),
        ]
    )
    return {
        "status": "withdrawn",
        "repository": repository,
        "base": base,
        "branch": branch,
        "pull_request": number,
        "url": pull_requests[0]["url"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update a review PR for one bounded Bureau automation branch."
    )
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--branch")
    parser.add_argument("--kind", choices=("source", "now-refill"), default="source")
    parser.add_argument("--auto-merge", action="store_true")
    parser.add_argument(
        "--publish",
        action="store_true",
        help=(
            "Before reconciling the pull request, compute and publish one bounded "
            "Now-refill proposal from real local Bureau truth. Only supported for "
            "--kind now-refill; requires --root."
        ),
    )
    parser.add_argument(
        "--root",
        help="Canonical Bureau checkout used to compute and publish --kind now-refill",
    )
    parser.add_argument("--state-db")
    parser.add_argument("--state-root")
    parser.add_argument("--floor", type=int, default=2)
    parser.add_argument("--target", type=int, default=4)
    parser.add_argument("--max-promotions", type=int, default=4)
    parser.add_argument("--authority", default="bureau-source-pr-bridge-local")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    branch = arguments.branch or (
        NOW_REFILL_BRANCH if arguments.kind == "now-refill" else DEFAULT_BRANCH
    )
    try:
        publish_result: dict[str, Any] | None = None
        if arguments.publish:
            if arguments.kind != "now-refill":
                raise SystemExit("--publish is only supported for --kind now-refill")
            if not arguments.root:
                raise SystemExit("--publish requires --root")
            publish_result = publish_now_refill(
                Path(arguments.root).expanduser().resolve(),
                state_db=Path(arguments.state_db).expanduser() if arguments.state_db else None,
                state_root=(
                    Path(arguments.state_root).expanduser() if arguments.state_root else None
                ),
                policy=NowRefillPolicy(
                    floor=arguments.floor,
                    target=arguments.target,
                    max_promotions=arguments.max_promotions,
                ),
                authority=arguments.authority,
                repository=arguments.repo,
                base=arguments.base,
                branch=branch,
            )
        if publish_result is not None and publish_result["status"] != "published":
            result = withdraw_open_proposal(arguments.repo, arguments.base, branch)
        else:
            result = reconcile(
                arguments.repo,
                arguments.base,
                branch,
                kind=arguments.kind,
                auto_merge=arguments.auto_merge,
            )
    except (
        GhCommandError,
        GitCommandError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise SystemExit(f"source PR bridge failed: {error}") from error
    if publish_result is not None:
        result = {"publish": publish_result, **result}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
