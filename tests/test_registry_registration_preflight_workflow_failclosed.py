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


def test_main_push_discovers_all_candidates_before_creating_checks() -> None:
    text = _workflow_text()

    discovery_marker = "# Discover the complete candidate set before publishing any CheckRun."
    publication_marker = "declare -A check_run_ids=()"

    assert discovery_marker in text
    assert publication_marker in text
    assert text.index(discovery_marker) < text.index(publication_marker)
    assert 'candidate_rows_file="$(mktemp)"' in text
    assert 'candidate_dir="$(mktemp -d)"' in text
    assert 'if [[ -s "${task_files_file}" ]]; then' in text
    assert 'mapfile -t candidate_rows < "${candidate_rows_file}"' in text
    assert 'for row in "${candidate_rows[@]}"; do' in text
    assert "for every known open PR" not in text


def test_main_push_discovery_errors_fail_before_check_publication() -> None:
    text = _workflow_text()

    discovery_failure = "if [[ ${candidate_discovery_failed} -ne 0 ]]; then"
    publication_marker = "declare -A check_run_ids=()"

    assert (
        'if ! gh api "repos/${REPOSITORY}/pulls/${pr_number}/files?per_page=100" --paginate'
        in text
    )
    assert 'echo "::error::Cannot inspect all changed files for PR #${pr_number}"' in text
    assert "candidate_discovery_failed=1" in text
    assert discovery_failure in text
    assert text.index(discovery_failure) < text.index(publication_marker)
    assert 'if ! gh api "repos/${REPOSITORY}/pulls?state=open&per_page=100" --paginate' in text


def test_main_push_never_reuses_partial_task_content_after_fetch_failure() -> None:
    text = _workflow_text()

    assert 'task_tmp="$(mktemp)"' in text
    assert '> "${task_tmp}"' in text
    assert 'rm -f "${task_tmp}"' in text
    assert 'mv "${task_tmp}" "${task_file}"' in text
