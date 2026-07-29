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
    assert "name: registry-registration-preflight/freshness" in text
    assert "statuses/${PR_HEAD_SHA}" not in text
    assert 'mapfile -t task_files < "${task_files_file}"' in text


def test_main_push_invalidates_all_known_prs_before_file_discovery() -> None:
    text = _workflow_text()

    invalidate_marker = (
        "# Create an in-progress CheckRun for every known open PR before "
        "validating any individual PR."
    )
    publication_marker = 'check_run_ids["${pr_number}"]="${check_run_id}"'
    discovery_marker = (
        'if ! gh api "repos/${REPOSITORY}/pulls/${pr_number}/files?per_page=100" '
        "--paginate"
    )

    assert invalidate_marker in text
    assert publication_marker in text
    assert discovery_marker in text
    assert text.index(invalidate_marker) < text.index(publication_marker)
    assert text.index(publication_marker) < text.index(discovery_marker)
    assert "mapfile -t pr_rows < <(" not in text
    assert "for attempt in 1 2 3; do" in text
    assert 'status:"in_progress"' in text


def test_main_push_file_discovery_failure_blocks_the_exact_pr() -> None:
    text = _workflow_text()

    discovery_marker = (
        'if ! gh api "repos/${REPOSITORY}/pulls/${pr_number}/files?per_page=100" '
        "--paginate"
    )
    failure_marker = (
        'complete_check_run \\\n'
        '                "${check_run_id}" failure \\\n'
        '                "Registry allocation revalidation errored"'
    )
    continue_marker = (
        'echo "::error::Cannot inspect changed files for PR #${pr_number}"\n'
        "              infrastructure_failed=1\n"
        "              continue"
    )

    assert discovery_marker in text
    assert failure_marker in text
    assert continue_marker in text
    assert text.index(discovery_marker) < text.index(failure_marker)
    assert text.index(failure_marker) < text.index(continue_marker)
    assert 'against main ${CURRENT_MAIN_SHA}." || true' in text
    assert "if [[ ${infrastructure_failed} -ne 0 ]]; then" in text


def test_main_push_skips_expensive_validation_for_non_candidates() -> None:
    text = _workflow_text()

    no_candidate_marker = 'if [[ ${#task_files[@]} -eq 0 ]]; then'
    success_marker = '"PR #${pr_number} has no new Registry task allocation'
    validation_marker = "bureau --root . --json registry-registration-preflight"

    assert no_candidate_marker in text
    assert success_marker in text
    assert text.index(
        validation_marker, text.index(no_candidate_marker)
    ) > text.index(no_candidate_marker)


def test_main_push_never_reuses_partial_task_content_after_fetch_failure() -> None:
    text = _workflow_text()

    assert 'task_tmp="$(mktemp)"' in text
    assert '> "${task_tmp}"' in text
    assert 'rm -f "${task_tmp}"' in text
    assert 'mv "${task_tmp}" "${task_file}"' in text
