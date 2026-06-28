from __future__ import annotations

import json

from bureau import source_pr_bridge


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, arguments, *, allow_not_found=False):
        self.calls.append((list(arguments), allow_not_found))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def encoded(value):
    return json.dumps(value)


def test_reconcile_returns_branch_absent(monkeypatch):
    runner = FakeRunner([None])
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile()
    assert result["status"] == "branch_absent"
    assert runner.calls[0][1] is True


def test_reconcile_returns_no_change_when_branch_is_not_ahead(monkeypatch):
    runner = FakeRunner(
        [
            encoded({"object": {"sha": "abc"}}),
            encoded({"ahead_by": 0}),
        ]
    )
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile()
    assert result["status"] == "no_change"
    assert result["head_sha"] == "abc"
    assert result["ahead_by"] == 0


def test_reconcile_creates_missing_pull_request(monkeypatch):
    runner = FakeRunner(
        [
            encoded({"object": {"sha": "abc"}}),
            encoded({"ahead_by": 1}),
            encoded([]),
            "https://github.com/heimgewebe/bureau/pull/9",
        ]
    )
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile()
    assert result["status"] == "created"
    assert result["head_sha"] == "abc"
    assert result["url"].endswith("/9")
    assert runner.calls[-1][0][:2] == ["pr", "create"]


def test_reconcile_updates_open_pull_request(monkeypatch):
    runner = FakeRunner(
        [
            encoded({"object": {"sha": "abc"}}),
            encoded({"ahead_by": 2}),
            encoded([{"number": 8, "url": "https://github.com/heimgewebe/bureau/pull/8"}]),
            "",
        ]
    )
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile()
    assert result["status"] == "updated"
    assert result["pull_request"] == 8
    assert runner.calls[-1][0][:3] == ["pr", "edit", "8"]
