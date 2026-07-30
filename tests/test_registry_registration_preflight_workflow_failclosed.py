import json
import subprocess
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
    assert text.count("blocking_publication_failed=1") == 4
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


def _recoverable_check_filter() -> str:
    text = _workflow_text()
    marker = "recoverable_check_filter='"
    start = text.index(marker) + len(marker)
    end = text.index("'\n            latest_recoverable_id=", start)
    return text[start:end]


def _run_recoverable_check_filter(
    check_runs: list[dict[str, object]],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "jq",
            "-r",
            "--arg",
            "name",
            "registry-registration-preflight/freshness",
            "--arg",
            "prefix",
            "registry-freshness:1197:exact-head:",
            _recoverable_check_filter(),
        ],
        input=json.dumps({"check_runs": check_runs}),
        text=True,
        capture_output=True,
        check=False,
    )


def _check_run(
    check_id: int,
    *,
    external_id: str,
    status: str,
    conclusion: str | None,
    name: str = "registry-registration-preflight/freshness",
) -> dict[str, object]:
    return {
        "id": check_id,
        "name": name,
        "external_id": external_id,
        "status": status,
        "conclusion": conclusion,
    }


def test_main_push_clears_latest_exact_recoverable_discovery_check_for_noncandidate() -> None:
    text = _workflow_text()
    helper = "clear_recoverable_discovery_check()"
    query = 'gh api -X GET "repos/${REPOSITORY}/commits/${sha}/check-runs"'
    prefix = 'prefix="registry-freshness:${pr_number}:${sha}:"'
    filter_expression = _recoverable_check_filter()
    failed_state = '.conclusion == "failure"'
    in_progress_state = '(.status == "in_progress" and .conclusion == null)'
    recovery_id = '"${latest_recoverable_id}" success'
    recovery_title = '"Registry discovery recovered"'
    noncandidate_call = (
        'elif ! clear_recoverable_discovery_check '
        '"${pr_head_sha}" "${pr_number}"; then'
    )
    assert text.count(helper) == 1
    assert query in text
    assert prefix in text
    assert filter_expression.index("startswith($prefix)") < filter_expression.index("max_by(.id)")
    assert failed_state in filter_expression
    assert in_progress_state in filter_expression
    assert recovery_id in text
    assert recovery_title in text
    assert noncandidate_call in text
    assert text.index(helper) < text.index(noncandidate_call)


def test_recoverable_filter_ignores_newer_same_name_foreign_prefix_checks() -> None:
    exact_prefix = "registry-freshness:1197:exact-head:"
    foreign_prefix = "registry-freshness:9999:exact-head:"
    cases = [
        _check_run(16, external_id=exact_prefix + "main", status="in_progress", conclusion=None),
        _check_run(
            17, external_id=foreign_prefix + "main", status="completed", conclusion="failure"
        ),
    ]
    result = _run_recoverable_check_filter(cases)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "16"

    cases[0] = _check_run(
        16, external_id=exact_prefix + "main", status="completed", conclusion="failure"
    )
    result = _run_recoverable_check_filter(cases)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "16"


def test_recoverable_filter_latest_exact_success_masks_older_exact_recoverable() -> None:
    exact_prefix = "registry-freshness:1197:exact-head:"
    for older_status, older_conclusion in [
        ("completed", "failure"),
        ("in_progress", None),
    ]:
        result = _run_recoverable_check_filter(
            [
                _check_run(
                    16,
                    external_id=exact_prefix + "old-main",
                    status=older_status,
                    conclusion=older_conclusion,
                ),
                _check_run(
                    18,
                    external_id=exact_prefix + "new-main",
                    status="completed",
                    conclusion="success",
                ),
            ]
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""


def test_recoverable_filter_ignores_newer_different_check_name() -> None:
    exact_prefix = "registry-freshness:1197:exact-head:"
    result = _run_recoverable_check_filter(
        [
            _check_run(
                16, external_id=exact_prefix + "main", status="completed", conclusion="failure"
            ),
            _check_run(
                19,
                external_id=exact_prefix + "main",
                status="completed",
                conclusion="failure",
                name="different-check",
            ),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "16"


def test_recoverable_filter_fails_closed_for_missing_check_run_collection() -> None:
    process = subprocess.run(
        [
            "jq",
            "-r",
            "--arg",
            "name",
            "registry-registration-preflight/freshness",
            "--arg",
            "prefix",
            "registry-freshness:1197:exact-head:",
            _recoverable_check_filter(),
        ],
        input="{}",
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.returncode != 0


def test_main_push_never_reuses_partial_task_content_after_fetch_failure() -> None:
    text = _workflow_text()

    assert 'task_tmp="$(mktemp)"' in text
    assert '> "${task_tmp}"' in text
    assert 'rm -f "${task_tmp}"' in text
    assert 'mv "${task_tmp}" "${task_file}"' in text
