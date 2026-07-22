from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from bureau import cli
from bureau.registry_registration_preflight import (
    RegistrationPreflightError,
    evaluate_registration_preflight,
    github_open_prs,
    repository_registration_preflight,
    task_path_for_id,
    validate_task_path,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
HEAD = "c" * 40


def task(
    task_id: str,
    title: str = "Registry collision guard",
    goal: str = "Prevent parallel task registration collisions",
) -> dict:
    return {"id": task_id, "title": title, "goal": goal}


def evaluate(**overrides):
    values = {
        "repository": "heimgewebe/bureau",
        "proposed_task": task("EXAMPLE-V1-T001"),
        "proposed_path": "registry/tasks/EXAMPLE-V1-T001.json",
        "checked_base_sha": SHA_A,
        "current_base_sha_before": SHA_A,
        "current_base_sha_after": SHA_A,
        "canonical_tasks": [],
        "open_prs": [],
        "pr_number": 10,
        "head_sha": HEAD,
    }
    values.update(overrides)
    return evaluate_registration_preflight(**values)


def test_allow_receipt_is_deterministic():
    first = evaluate()
    second = evaluate()
    assert first == second
    assert first["decision"] == "allow"
    assert first["reasons"] == ["registration_slot_available"]
    assert len(first["decision_sha256"]) == 64


def test_stale_base_blocks_fail_closed():
    result = evaluate(current_base_sha_before=SHA_B, current_base_sha_after=SHA_B)
    assert result["decision"] == "block"
    assert "stale_base" in result["reasons"]


def test_base_change_during_preflight_blocks_fail_closed():
    result = evaluate(current_base_sha_after=SHA_B)
    assert result["decision"] == "block"
    assert "base_changed_during_preflight" in result["reasons"]


def test_canonical_collision_is_distinct():
    result = evaluate(canonical_tasks=[task("EXAMPLE-V1-T001")])
    assert result["decision"] == "block"
    assert result["collisions"] == [
        {
            "source": "canonical_main",
            "task_id": "EXAMPLE-V1-T001",
            "path": "registry/tasks/EXAMPLE-V1-T001.json",
        }
    ]


def test_open_pr_reservation_blocks_but_current_pr_is_excluded():
    reservation = {
        "number": 11,
        "head_sha": SHA_B,
        "task_paths": ["registry/tasks/EXAMPLE-V1-T001.json"],
        "tasks": [],
    }
    blocked = evaluate(open_prs=[reservation])
    assert blocked["decision"] == "block"
    assert blocked["collisions"][0]["source"] == "open_pr"
    allowed = evaluate(open_prs=[{**reservation, "number": 10}])
    assert allowed["decision"] == "allow"


def test_semantic_duplicate_is_hint_only():
    proposed = task(
        "EXAMPLE-V1-T001",
        "Registry task collision guard",
        "Prevent parallel Registry task ID collision during registration",
    )
    existing = task(
        "EXAMPLE-V1-T002",
        "Registry task collision prevention",
        "Prevent parallel Registry task ID collision during publication",
    )
    result = evaluate(proposed_task=proposed, canonical_tasks=[existing])
    assert result["decision"] == "allow"
    assert result["semantic_hints"][0]["kind"] == "possible_duplicate"
    assert result["semantic_hints"][0]["task_id"] == "EXAMPLE-V1-T002"


def test_invalid_task_path_and_traversal_are_rejected():
    with pytest.raises(RegistrationPreflightError):
        validate_task_path("EXAMPLE-V1-T001", "registry/tasks/../EXAMPLE-V1-T001.json")
    with pytest.raises(RegistrationPreflightError):
        task_path_for_id("../EXAMPLE-V1-T001")


def test_concurrency_regression_second_attempt_blocks_after_reservation():
    first = evaluate()
    assert first["decision"] == "allow"
    second = evaluate(
        open_prs=[
            {
                "number": 11,
                "head_sha": SHA_B,
                "task_paths": ["registry/tasks/EXAMPLE-V1-T001.json"],
                "tasks": [task("EXAMPLE-V1-T001")],
            }
        ]
    )
    assert second["decision"] == "block"
    assert second["decision_sha256"] != first["decision_sha256"]


def test_github_open_pr_provider_is_injectable_and_paginated():
    calls: list[list[str]] = []
    proposed = task(
        "EXAMPLE-V1-T002",
        "Registry task collision prevention",
        "Prevent parallel Registry task ID collision during publication",
    )
    encoded = base64.b64encode(json.dumps(proposed).encode("utf-8")).decode("ascii")

    def runner(arguments: list[str]) -> str:
        calls.append(arguments)
        target = arguments[2]
        if target.endswith("pulls?state=open&per_page=100"):
            return f"10\t{HEAD}\n11\t{SHA_B}"
        if target.endswith("pulls/11/files?per_page=100"):
            return "README.md\nregistry/tasks/EXAMPLE-V1-T002.json"
        if target.endswith(f"contents/registry/tasks/EXAMPLE-V1-T002.json?ref={SHA_B}"):
            return json.dumps({"content": encoded})
        raise AssertionError(arguments)

    result = github_open_prs(
        "heimgewebe/bureau",
        current_pr_number=10,
        runner=runner,
    )
    assert result == [
        {
            "number": 11,
            "head_sha": SHA_B,
            "task_paths": ["registry/tasks/EXAMPLE-V1-T002.json"],
            "tasks": [proposed],
        }
    ]
    paginated_calls = [call for call in calls if "pulls" in call[2]]
    assert len(paginated_calls) == 2
    assert all("--paginate" in call for call in paginated_calls)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def test_repository_preflight_reads_canonical_tasks_from_checked_revision(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "registry/tasks").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@invalid.local"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "registry/tasks/EXISTING-V1-T001.json").write_text(
        json.dumps(task("EXISTING-V1-T001")), encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = _git(repo, "rev-parse", "HEAD")
    proposed_path = repo / "registry/tasks/EXAMPLE-V1-T001.json"
    proposed_path.write_text(json.dumps(task("EXAMPLE-V1-T001")), encoding="utf-8")
    result = repository_registration_preflight(
        repo,
        repository="heimgewebe/bureau",
        task_json_path=proposed_path,
        checked_base_sha=base,
        open_pr_provider=lambda _repo, _number: [],
        base_sha_provider=lambda _root, _ref: base,
    )
    assert result["decision"] == "allow"


def test_cli_json_and_exit_codes(monkeypatch, tmp_path: Path, capsys):
    receipt = evaluate()
    monkeypatch.setattr(
        "bureau.registry_registration_preflight.repository_registration_preflight",
        lambda *args, **kwargs: receipt,
    )
    receipt_path = tmp_path / "receipt.json"
    exit_code = cli.main(
        [
            "--root",
            str(tmp_path),
            "--json",
            "registry-registration-preflight",
            "--repo-slug",
            "heimgewebe/bureau",
            "--task-json",
            "registry/tasks/EXAMPLE-V1-T001.json",
            "--checked-base-sha",
            SHA_A,
            "--receipt-out",
            str(receipt_path),
        ]
    )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "allow"
    assert json.loads(receipt_path.read_text())["decision_sha256"] == receipt["decision_sha256"]

    blocked = {**receipt, "decision": "block", "reasons": ["stale_base"]}
    monkeypatch.setattr(
        "bureau.registry_registration_preflight.repository_registration_preflight",
        lambda *args, **kwargs: blocked,
    )
    exit_code = cli.main(
        [
            "--root",
            str(tmp_path),
            "--json",
            "registry-registration-preflight",
            "--repo-slug",
            "heimgewebe/bureau",
            "--task-json",
            "registry/tasks/EXAMPLE-V1-T001.json",
            "--checked-base-sha",
            SHA_A,
        ]
    )
    assert exit_code == 2


def test_required_check_catalog_makes_registry_freshness_part_of_readiness():
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / ".github/grabowski-required-checks.json").read_text())
    assert catalog == {
        "schema_version": 1,
        "required_checks": [
            "validate (3.10)",
            "validate (3.12)",
            "registry-registration-preflight/freshness",
        ],
    }


def test_workflow_uses_trusted_base_code_and_revalidates_on_main_push():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/registry-registration-preflight.yml").read_text()
    lines = text.splitlines()
    required = [
        "  pull_request_target:",
        "  push:",
        "  statuses: write",
        "github.event_name == 'pull_request_target'",
        "github.event_name == 'push'",
        "ref: ${{ github.event.pull_request.base.sha }}",
        "HEAD_REPOSITORY: ${{ github.event.pull_request.head.repo.full_name }}",
        "registry-registration-preflight/freshness",
        "?base_sha=${CHECKED_BASE_SHA}",
        "?base_sha=${CURRENT_MAIN_SHA}",
        "CURRENT_MAIN_SHA",
        "--checked-base-sha \"${CURRENT_MAIN_SHA}\"",
        "pulls?state=open&per_page=100",
        "pulls/${pr_number}/files?per_page=100",
        "statuses/${sha}",
        "No new Registry task allocation on current main",
        "repos/${HEAD_REPOSITORY}/contents/${task_file}?ref=${PR_HEAD_SHA}",
        "repos/${pr_head_repository}/contents/${task_file}?ref=${pr_head_sha}",
        "^registry/tasks/[A-Za-z0-9][A-Za-z0-9._:-]{0,239}",
    ]
    missing = [token for token in required if token not in text]
    assert not missing
    assert "  pull_request:" not in lines
    assert "    paths:" not in lines
    assert text.count("--paginate") >= 3
    assert text.count("registry-registration-preflight/freshness") >= 2
    assert "task_id=\"$(python" not in text
