from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/sync-weltgewebe-source.yml"


def test_source_sync_workflow_contract():
    text = WORKFLOW.read_text(encoding="utf-8")
    required = (
        'cron: "0,30 * * * *"',
        "workflow_dispatch:",
        "cancel-in-progress: false",
        "source-sync weltgewebe",
        "--apply",
        'python -m bureau.source_sync_output "$report"',
        "registry/sources/weltgewebe.json",
        "make validate",
        "Unexpected changed paths",
        "contents: write",
        "--force-with-lease=",
        "Publish snapshot branch",
        "The local Bureau source PR bridge will create or update the review PR.",
    )
    for value in required:
        assert value in text
    assert "report['changed']" not in text
    assert "report['commit_sha']" not in text
    assert "report['document_sha256']" not in text
    assert "pull-requests: write" not in text
    assert "gh pr create" not in text
    assert "gh pr edit" not in text
    assert "gh pr merge" not in text
    assert "HEAD:refs/heads/main" not in text
