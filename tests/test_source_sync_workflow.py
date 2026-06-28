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
        "registry/sources/weltgewebe.json",
        "make validate",
        "Unexpected changed paths",
        "pull-requests: write",
        "--force-with-lease=",
        "gh pr create",
        "gh pr edit",
    )
    for value in required:
        assert value in text
    assert "gh pr merge" not in text
    assert "HEAD:refs/heads/main" not in text
