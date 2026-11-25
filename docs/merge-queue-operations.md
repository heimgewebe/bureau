# Bureau merge queue operations

The `main` branch is protected by repository ruleset `20228857` (`Bureau main strict base-update guard`). The ruleset keeps these required checks:

- `validate (3.10)`
- `validate (3.12)`
- `registry-registration-preflight/freshness`

The ruleset also enables the GitHub merge queue. Queue entries are integrated serially (`max_entries_to_merge=1`) using merge commits. GitHub evaluates the required checks on the generated merge-group commit, including any preceding queued changes. This avoids repeated manual branch updates within the queue.

Both required workflow surfaces already accept `merge_group` events:

- `.github/workflows/validate.yml`
- `.github/workflows/registry-registration-merge-group.yml`

## Live readback

```bash
gh api repos/heimgewebe/bureau/rulesets/20228857
```

The readback must show:

- active enforcement on `refs/heads/main`;
- no bypass actors;
- the three required checks above;
- one active required-status-check rule; the original T025 acceptance used `strict_required_status_checks_policy=false`, but GitHub ruleset history records a change to `true` on 2026-08-04. Do not infer the reason or silently change this setting;
- one `merge_queue` rule with `max_entries_to_merge=1`.

A pull request may enter the queue after its exact head is reviewed and the required pull-request checks are green. For code changes to Bureau's `main`, the PR checks run real admission validation (Ruff, Systemkatalog boundary, Registry check, and representative Bureau/CI tests) when the active merge-queue rule is confirmed. If rule discovery fails, the queue is absent, or the target differs, the normal full validation runs. The existing task-only and queue-only Registry fast paths are unchanged.

Admission is not full integration evidence. The merge-group commit still runs the complete `make validate` on both Python versions for full-scope changes. All three required check names remain stable. Do not interpret a PR admission success as a complete test-suite result.

For a main push, the validate workflow may reuse an exact successful full merge-group run for the same commit. It checks both matrix jobs and their actual full-validation step using the repository Actions API and the recorded run attempt. It still installs dependencies, runs admission/smoke validation, and checks the accepted-main source-precondition authority history (which intentionally differs from merge-group history). If the proof is missing, ambiguous, incomplete, unavailable, or only covers a Registry fast path, main runs `make validate` as before. Registry main-push revalidation and all other workflows are unchanged. No separate evidence store or merge-queue bypass is introduced.

## Failure and recovery

A conflict, missing check, red check, timeout or cancelled queue entry blocks integration without changing the pull-request branch. Re-run or re-enqueue only after reading the current pull-request head, mergeability and required check set. Do not force-push a foreign branch or bypass the ruleset.

If the queue itself must be rolled back, first restore full PR validation and ensure outstanding PRs have passed it, then remove the `merge_queue` rule and retain `strict_required_status_checks_policy=true`, required checks and empty bypass list. Verify the exact ruleset readback immediately after the update. Never administratively merge an admission-only PR around the queue.

This operational setting does not by itself complete `BUREAU-TRUTH-MODEL-V2-T025`. Completion additionally requires revisions bound to two conflict-free queued pull requests, final merge-group check evidence, merge commits, failure/recovery evidence, and a post-merge readback.
