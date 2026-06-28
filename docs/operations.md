# Operations

## Diagnose

```bash
bureau --root . doctor --json
bureau --root . lifecycle --json
bureau --root . explain-next --capability repository --capability shell --json
```

## Check out work

```bash
bureau --root . checkout-next --worker <stable-session-id> \
  --capability repository --capability shell --json
```

Use a stable session ID. Repeating the command returns the existing active assignment rather than
claiming another task.

## Complete

Evidence is a JSON object keyed by acceptance criterion ID:

```bash
bureau --root . complete <run-id> --evidence evidence.json --json
```

Completion is idempotent. SQLite is canonical; the receipt file is a deterministic materialisation.

## Workspaces

```bash
bureau --root . workspace-status <run-id> --json
bureau --root . workspace-preserve <run-id> --reason 'needs review'
bureau --root . workspace-cleanup <run-id>
```

Cleanup requires a terminal run. Dirty or unmerged workspaces are preserved unless `--force` is
explicitly supplied.


## Weltgewebe source inbox

Validate the locally available source ref without changing Bureau state:

```bash
bureau --root . --json source-check weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main
```

Preview drift against the currently materialised source snapshot:

```bash
bureau --root . --json source-sync weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main
```

Apply the validated snapshot atomically:

```bash
bureau --root . --json source-sync weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main --apply
```

The adapter performs no fetch and makes no network request. It ignores repository pager, hook and
external-diff configuration, validates both source documents from the same resolved commit, and
bounds preview ID lists. Repeating `--apply` for unchanged source bytes performs no write.

Scheduling may run `source-check`, preview sync or reconciliation. It must not imply promotion,
readiness or approval to execute any source task.


## Scheduled Weltgewebe synchronization

The `sync-weltgewebe-source` GitHub workflow runs at minute 0 and 30 of every hour and can also be started manually. It checks out the current public Weltgewebe `main`, materializes the candidate snapshot in an ephemeral Bureau checkout, and runs the full validation suite when the snapshot changes.

A changed snapshot is pushed only to the bot-owned `automation/weltgewebe-source-sync` branch using an explicit force-with-lease precondition. The workflow creates or updates a pull request; it never pushes to `main` and never merges the pull request. Only `registry/sources/weltgewebe.json` may change. Any additional changed path fails the run.

GitHub schedules are best-effort rather than exact wall-clock execution. A delayed run affects freshness only; it does not weaken commit binding or permit task promotion. Repository Actions settings must allow `GITHUB_TOKEN` to create pull requests, otherwise publication fails visibly while validation remains read-only.
