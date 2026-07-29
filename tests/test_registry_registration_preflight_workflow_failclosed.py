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

    discovery_marker = "# Discover the complete candidate set before normal CheckRun publication."
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


def test_main_push_discovery_errors_publish_blocking_checks_before_exit() -> None:
    text = _workflow_text()

    helper = "publish_discovery_failure()"
    discovery_failure = (
        "if [[ ${candidate_discovery_failed} -ne 0 || "
        "${blocking_publication_failed} -ne 0 ]]; then"
    )
    normal_publication = "declare -A check_run_ids=()"

    assert text.count(helper) == 1
    assert (
        'if ! gh api "repos/${REPOSITORY}/pulls/${pr_number}/files?per_page=100" --paginate'
        in text
    )
    assert '"Registry freshness discovery errored"' in text
    assert 'echo "::error::Cannot inspect all changed files for PR #${pr_number}"' in text
    assert (
        'echo "::error::Registry candidate PR #${pr_number} has no readable head '
        'repository"'
        in text
    )
    candidate_map = 'mapfile -t candidate_rows < "${candidate_rows_file}"'
    assert text.count("if ! publish_discovery_failure \\") == 3
    assert text.count("blocking_publication_failed=1") == 3
    assert discovery_failure in text
    assert (
        "Registry candidate discovery was incomplete while checking PR #${pr_number}"
        in text
    )
    assert text.count('for row in "${candidate_rows[@]}"; do') == 3
    assert (
        text.index(helper)
        < text.index(candidate_map)
        < text.index(discovery_failure)
        < text.index(normal_publication)
    )
    assert 'if ! gh api "repos/${REPOSITORY}/pulls?state=open&per_page=100" --paginate' in text


def test_main_push_never_reuses_partial_task_content_after_fetch_failure() -> None:
    text = _workflow_text()

    assert 'task_tmp="$(mktemp)"' in text
    assert '> "${task_tmp}"' in text
    assert 'rm -f "${task_tmp}"' in text
    assert 'mv "${task_tmp}" "${task_file}"' in text
