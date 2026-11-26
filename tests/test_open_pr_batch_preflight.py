import json
import subprocess

import pytest

from bureau import v2


def result(data, errors=(), rc=0, stderr=""):
    return subprocess.CompletedProcess(
        [], rc, json.dumps({"data": data, "errors": list(errors)}), stderr
    )


def row(repo, count):
    return {"nameWithOwner": repo, "pullRequests": {"totalCount": count}}


def missing(alias, repo):
    return {
        "type": "NOT_FOUND",
        "path": [alias],
        "message": f"Could not resolve to a Repository with the name '{repo}'.",
    }


def test_mixed(monkeypatch):
    repos = ["o/empty", "o/missing", "o/active", "o/alias", "o/bad"]
    monkeypatch.setattr(
        v2.subprocess,
        "run",
        lambda *a, **k: result(
            {
                "r0": row(repos[0], 0),
                "r1": None,
                "r2": row(repos[2], 2),
                "r3": row("o/renamed", 0),
                "r4": row(repos[4], False),
            },
            [missing("r1", repos[1])],
            1,
        ),
    )
    empty, errors, primary = v2._batch_empty_open_pr_observation(repos)
    assert empty == {repos[0]: []}
    assert set(errors) == {repos[1]}
    assert not primary


@pytest.mark.parametrize(
    "err",
    [
        {"type": "FORBIDDEN", "path": ["r0"]},
        {"type": "NOT_FOUND", "path": ["r0", "pullRequests"], "message": "wrong"},
        {"type": "NOT_FOUND", "path": ["r0"], "message": "wrong"},
        {"type": "NOT_FOUND", "path": ["unknown"]},
        {},
    ],
)
def test_unknown_is_never_empty_or_confirmed_missing(monkeypatch, err):
    monkeypatch.setattr(v2.subprocess, "run", lambda *a, **k: result({"r0": None}, [err], 1))
    assert v2._batch_empty_open_pr_observation(["o/a"]) == ({}, {}, False)


@pytest.mark.parametrize(
    "data",
    [
        {"r0": row("o/a", False)},
        {"r0": row("o/b", 0)},
        {"r0": row(None, 0)},
        {"r0": row(123, 0)},
        {"r0": None},
        {},
        {"r0": {"nameWithOwner": "o/a", "pullRequests": {}}},
    ],
)
def test_incomplete_or_alias_not_skipped(monkeypatch, data):
    monkeypatch.setattr(v2.subprocess, "run", lambda *a, **k: result(data))
    assert v2._batch_empty_open_pr_observation(["o/a"]) == ({}, {}, False)


def test_primary(monkeypatch):
    monkeypatch.setattr(
        v2.subprocess, "run", lambda *a, **k: result({}, [], 1, "GraphQL: API rate limit exceeded")
    )
    assert v2._batch_empty_open_pr_observation(["o/a"]) == ({}, {}, True)


def test_secondary(monkeypatch):
    monkeypatch.setattr(
        v2.subprocess,
        "run",
        lambda *a, **k: result({}, [], 1, "GraphQL: secondary rate limit exceeded"),
    )
    empty, errors, primary = v2._batch_empty_open_pr_observation(["o/a", "o/b"])
    assert not empty and set(errors) == {"o/a", "o/b"} and not primary


def test_partial_not_found_with_verbose_http_headers(monkeypatch):
    repos = ["o/empty", "o/missing", "o/live"]
    stderr = "Access-Control-Expose-Headers: X-RateLimit-Remaining, Retry-After\n" + "gh: missing"
    monkeypatch.setattr(
        v2.subprocess,
        "run",
        lambda *a, **k: result(
            {"r0": row(repos[0], 0), "r1": None, "r2": row(repos[2], 3)},
            [missing("r1", repos[1])],
            1,
            stderr,
        ),
    )
    empty, failed, primary = v2._batch_empty_open_pr_observation(repos)
    assert empty == {repos[0]: []} and set(failed) == {repos[1]} and not primary


def test_partial_secondary_is_scoped(monkeypatch):
    repos = ["o/empty", "o/throttled", "o/live"]
    err = {"path": ["r1"], "type": "RATE_LIMITED", "message": "secondary rate limit exceeded"}
    monkeypatch.setattr(
        v2.subprocess,
        "run",
        lambda *a, **k: result(
            {"r0": row(repos[0], 0), "r1": None, "r2": row(repos[2], 3)},
            [err],
            1,
            "gh: secondary rate limit exceeded",
        ),
    )
    empty, failed, primary = v2._batch_empty_open_pr_observation(repos)
    assert empty == {repos[0]: []} and set(failed) == {repos[1]} and not primary


def test_partial_primary_keeps_positive_observation(monkeypatch):
    repos = ["o/empty", "o/limited"]
    err = {"path": ["r1"], "type": "RATE_LIMITED", "message": "API rate limit exceeded"}
    monkeypatch.setattr(
        v2.subprocess, "run", lambda *a, **k: result({"r0": row(repos[0], 0), "r1": None}, [err], 1)
    )
    empty, failed, primary = v2._batch_empty_open_pr_observation(repos)
    assert empty == {repos[0]: []} and failed == {} and primary


def test_full_observation_with_mixed_batch(monkeypatch):
    repos = [f"o/r{i}" for i in range(46)]
    calls = []

    def batch(repositories):
        calls.append(("batch", tuple(repositories)))
        return (
            {r: [] for r in repositories if int(r.split("/r")[1]) % 3 == 0},
            {r: "unavailable" for r in repositories if int(r.split("/r")[1]) % 3 == 1},
            False,
        )

    def existing(r, **kwargs):
        calls.append(("existing", r))
        return [{"number": 1}]

    monkeypatch.setattr(v2, "_batch_empty_open_pr_observation", batch)
    monkeypatch.setattr(v2, "_github_open_pull_requests", existing)
    observed, errors = v2._observe_open_pull_requests(repos)
    assert len(observed) == 31 and len(errors) == 15 and set(observed) | set(errors) == set(repos)
    assert (
        len([c for c in calls if c[0] == "batch"]) == 2
        and len([c for c in calls if c[0] == "existing"]) == 15
    )


def test_full_primary_latch(monkeypatch):
    repos = [f"o/r{i}" for i in range(46)]
    batch = []
    existing = []

    def probe(r):
        batch.append(r)
        return {}, {}, True

    def observe(r, **kw):
        assert kw["graphql_primary_exhausted"].is_set()
        existing.append(r)
        return []

    monkeypatch.setattr(v2, "_batch_empty_open_pr_observation", probe)
    monkeypatch.setattr(v2, "_github_open_pull_requests", observe)
    seen, errors = v2._observe_open_pull_requests(repos)
    assert len(batch) == 1 and len(existing) == 46 and len(seen) == 46 and not errors
