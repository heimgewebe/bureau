from pathlib import Path


def _workflow_text() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / ".github/workflows/registry-registration-preflight.yml").read_text(
        encoding="utf-8"
    )


def test_pull_request_file_listing_fails_closed() -> None:
    text = _workflow_text()

    assert "mapfile -t task_files < <(" not in text
    assert 'task_files_file="$(mktemp)"' in text
    assert (
        'if ! gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}/files?per_page=100" --paginate'
        in text
    )
    assert '"Registry allocation preflight errored"' in text
    assert 'mapfile -t task_files < "${task_files_file}"' in text


def test_main_push_invalidates_all_known_prs_before_per_pr_validation() -> None:
    text = _workflow_text()

    invalidate_marker = "# Invalidate every known open PR before validating any individual PR."
    validation_marker = "infrastructure_failed=0"

    assert invalidate_marker in text
    assert validation_marker in text
    assert text.index(invalidate_marker) < text.index(validation_marker)
    assert "mapfile -t pr_rows < <(" not in text
    assert "for attempt in 1 2 3; do" in text
    assert '"Registry allocation revalidation running"' in text
    assert "pending" in text


def test_main_push_continues_after_one_pr_revalidation_error() -> None:
    text = _workflow_text()

    assert (
        'if ! gh api "repos/${REPOSITORY}/pulls/${pr_number}/files?per_page=100" --paginate'
        in text
    )
    assert '"Registry allocation revalidation errored" || true' in text
    assert 'echo "::error::Cannot inspect changed files for PR #${pr_number}"' in text
    assert "infrastructure_failed=1" in text
    assert "continue" in text
    assert "if [[ ${infrastructure_failed} -ne 0 ]]; then" in text


def test_main_push_never_reuses_partial_task_content_after_fetch_failure() -> None:
    text = _workflow_text()

    assert 'task_tmp="$(mktemp)"' in text
    assert '> "${task_tmp}"' in text
    assert 'rm -f "${task_tmp}"' in text
    assert 'mv "${task_tmp}" "${task_file}"' in text
