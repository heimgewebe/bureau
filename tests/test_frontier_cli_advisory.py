"""The advisory task finder must not spend GitHub API quota."""
from __future__ import annotations

import json

import pytest

from bureau import cli as bureau_cli
from bureau import v2 as bureau_v2


@pytest.mark.parametrize("live", [False, True])
def test_frontier_cli_pr_observation_is_explicit(
    registry_factory, tmp_path, monkeypatch, capsys, live
):
    root = registry_factory(1, mode="write")
    monkeypatch.setenv("BUREAU_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "1")
    monkeypatch.setattr(
        bureau_v2, "_github_repository_for_path", lambda _path: "heimgewebe/example"
    )
    calls = []

    def observe(repository, **_kwargs):
        calls.append(repository)
        return []

    monkeypatch.setattr(bureau_v2, "_github_open_pull_requests", observe)
    args = ["--root", str(root), "--json", "frontier", "--capability", "repository"]
    if live:
        args.append("--live-pr")
    assert bureau_cli.main(args) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["task_id"] == "BUR-TEST-001-T001"
    assert rows[0]["effective_state"] == "ready"
    assert calls == (["heimgewebe/example"] if live else [])
    if live:
        assert rows[0]["eligible"] is True
    else:
        assert rows[0]["eligible"] is False
        assert rows[0]["open_pr_scope"] is None
        assert "unobserved" in " ".join(rows[0]["claim_reasons"])
