import os
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_retired_claude_workflow_is_absent() -> None:
    assert not (ROOT / ".github/workflows/claude.yml").exists()


def test_validate_concurrency_is_pr_bound_without_cross_event_cancellation() -> None:
    text = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")

    assert "group: validate-${{ github.event_name }}-" in text
    assert "github.event.pull_request.number" in text
    assert "github.event.merge_group.head_sha" in text
    assert "github.ref" in text
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text
    assert 'python-version: ["3.10", "3.12"]' in text


def test_registry_preflight_concurrency_separates_pr_and_main_push() -> None:
    text = (
        ROOT / ".github/workflows/registry-registration-preflight.yml"
    ).read_text(encoding="utf-8")

    assert (
        "group: registry-registration-preflight-pr-"
        "${{ github.event.pull_request.number }}" in text
    )
    assert "group: registry-registration-preflight-main-push" in text
    assert text.count("cancel-in-progress: true") == 1
    assert text.count("cancel-in-progress: false") == 1
    assert "name: registry-registration-preflight/freshness" in text


def test_required_check_names_remain_stable() -> None:
    validate = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
    registry = (
        ROOT / ".github/workflows/registry-registration-preflight.yml"
    ).read_text(encoding="utf-8")

    assert "name: validate" in validate
    assert 'python-version: ["3.10", "3.12"]' in validate
    assert "name: registry-registration-preflight/freshness" in registry


@pytest.mark.parametrize(
    ("event", "base", "repo", "reply", "exit_code", "expected", "probe"),
    [
        ("pull_request", "main", "heimgewebe/bureau", "true", 0, "admission", True),
        ("pull_request", "main", "heimgewebe/bureau", "false", 0, "full", True),
        ("pull_request", "main", "heimgewebe/bureau", "null", 0, "full", True),
        ("pull_request", "main", "heimgewebe/bureau", "true", 1, "full", True),
        ("pull_request", "main", "heimgewebe/bureau", "", 124, "full", True),
        ("pull_request", "release", "heimgewebe/bureau", "true", 0, "full", False),
        ("pull_request", "main", "example/bureau", "true", 0, "full", False),
        ("merge_group", "", "heimgewebe/bureau", "true", 0, "full", False),
        ("push", "", "heimgewebe/bureau", "true", 0, "full", False),
    ],
)
def test_validation_tier_uses_real_workflow_script(
    tmp_path, event, base, repo, reply, exit_code, expected, probe
):
    workflow = (ROOT / ".github/workflows/validate.yml").read_text()
    step = workflow.split("- name: Choose validation tier\n", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    script = dedent(step.split("        run: |\n", 1)[1])
    marker = tmp_path / "gh-called"
    output = tmp_path / "github-output"
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" > "$GH_MARKER"\n'
        'printf "%s\\n" "$GH_REPLY"\n'
        'exit "$GH_EXIT_CODE"\n'
    )
    gh.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "EVENT_NAME": event,
        "BASE_REF": base,
        "REPOSITORY": repo,
        "GITHUB_OUTPUT": str(output),
        "GH_MARKER": str(marker),
        "GH_REPLY": reply,
        "GH_EXIT_CODE": str(exit_code),
    }
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
    assert output.read_text() == f"tier={expected}\n"
    assert marker.exists() is probe
    if probe:
        assert "repos/heimgewebe/bureau/rules/branches/main" in marker.read_text()


def test_validation_tiers_preserve_full_final_check_and_real_admission_checks():
    workflow = (ROOT / ".github/workflows/validate.yml").read_text()
    makefile = (ROOT / "Makefile").read_text()
    assert "steps.scope.outputs.scope_kind == 'full' && steps.tier.outputs.tier != 'admission'" in (
        workflow
    )
    assert "steps.scope.outputs.scope_kind == 'full' && steps.tier.outputs.tier == 'admission'" in (
        workflow
    )
    assert "run: make validate\n" in workflow
    assert "run: make validate-admission\n" in workflow
    assert "validate: lint test systemkatalog-boundary" in makefile
    assert "validate-admission: lint systemkatalog-boundary" in makefile
    assert "PYTHONPATH=src python -m bureau.cli --root . check" in makefile
    assert "PYTHONPATH=src pytest tests/test_bureau.py" in makefile
