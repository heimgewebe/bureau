from pathlib import Path

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
