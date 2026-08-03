# Bureau merge queue operations

The `main` branch is protected by repository ruleset `20228857` (`Bureau main strict base-update guard`). The ruleset keeps these required checks:

- `validate (3.10)`
- `validate (3.12)`
- `registry-registration-preflight/freshness`

The ruleset also enables the GitHub merge queue. Queue entries are integrated serially (`max_entries_to_merge=1`) using merge commits. GitHub evaluates the required checks on the generated merge-group commit. Pull-request heads therefore do not need a manual update after every unrelated `main` advance.

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
- `strict_required_status_checks_policy=false`;
- one `merge_queue` rule with `max_entries_to_merge=1`.

A pull request is merge-ready only after its exact head is reviewed and its required pull-request checks are green. Queue admission does not reuse those checks as final integration evidence: the queue-generated merge-group commit must pass the same required check contexts.

## Failure and recovery

A conflict, missing check, red check, timeout or cancelled queue entry blocks integration without changing the pull-request branch. Re-run or re-enqueue only after reading the current pull-request head, mergeability and required check set. Do not force-push a foreign branch or bypass the ruleset.

If the queue itself must be rolled back, update only ruleset `20228857`: remove the `merge_queue` rule and restore `strict_required_status_checks_policy=true`, while preserving the required checks and empty bypass list. Verify the exact ruleset readback immediately after the update.

This operational setting does not by itself complete `BUREAU-TRUTH-MODEL-V2-T025`. Completion additionally requires revisions bound to two conflict-free queued pull requests, final merge-group check evidence, merge commits, failure/recovery evidence, and a post-merge readback.
