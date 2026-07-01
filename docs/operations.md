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

The `sync-weltgewebe-source` GitHub workflow runs at minute 0 and 30 of every hour and can also be
started manually. It checks out the current public Weltgewebe `main`, materialises the candidate
snapshot in an ephemeral Bureau checkout, and runs the full validation suite when the snapshot
changes.

A changed snapshot is pushed only to the bot-owned `automation/weltgewebe-source-sync` branch using
an explicit force-with-lease precondition. The workflow never pushes to `main`, merges a pull
request or promotes a source task. Only `registry/sources/weltgewebe.json` may change; any additional
changed path fails the run.

The Heimgewebe organisation deliberately prevents `GITHUB_TOKEN` from creating pull requests. The
least-privilege design therefore keeps branch publication in GitHub Actions and delegates pull
request creation to the local `bureau-source-pr-bridge`, which uses the already authorised user
`gh` session without exporting its token to GitHub Actions.

Install the bridge into an isolated environment and enable the supplied user timer:

```bash
python3 -m venv ~/.local/share/bureau-source-pr-bridge/venv
~/.local/share/bureau-source-pr-bridge/venv/bin/pip install .
install -Dm644 ops/systemd/bureau-source-pr-bridge.service \
  ~/.config/systemd/user/bureau-source-pr-bridge.service
install -Dm644 ops/systemd/bureau-source-pr-bridge.timer \
  ~/.config/systemd/user/bureau-source-pr-bridge.timer
systemctl --user daemon-reload
systemctl --user enable --now bureau-source-pr-bridge.timer
```

The timer runs at minute 15 and 45, after the hosted source observation. A delayed hosted run is
picked up by a later bridge run. The bridge is idempotent: it does nothing without an ahead source
branch, creates a missing review pull request, and otherwise refreshes the existing pull request
body.

Manual checks:

```bash
bureau-source-pr-bridge
systemctl --user status bureau-source-pr-bridge.timer
journalctl --user -u bureau-source-pr-bridge.service -n 50 --no-pager
```

Neither half of the pipeline establishes readiness, dependency completeness, safe parallel scope or
autonomous execution permission.

## Source promotion preview

Plan one Weltgewebe task candidate without materialising it:

```bash
bureau --root . --json source-promote-plan weltgewebe --task-id DEPLOY-DNS-001
```

The result is read-only. It exposes the projected Bureau task ID, source binding, unresolved claims,
unknown dependency structure and execution policy decisions. A promotion preview does not imply
readiness or permission to execute.

## Local Review Steward

The `bureau-review-steward` command performs a local, read-mostly review pass over the current
Closure state. It reads `lanes.json`, `plan.json`, generated Grabowski briefs, repository diff state
and, when `gh` is available, pull-request review and check evidence. It writes only lane review
evidence and review receipts under the Closure state root. It never starts coding work and never
merges.

Manual run:

```bash
bureau-review-steward run
```

The command prints a compact receipt summary by default. Use `--full-json` only when the full
lane evidence needs to be inspected outside the receipt file.

Install the steward into an isolated environment and enable the supplied user timer:

```bash
python3 -m venv ~/.local/share/bureau-review-steward/venv
~/.local/share/bureau-review-steward/venv/bin/pip install .
install -Dm644 ops/systemd/bureau-review-steward.service \
  ~/.config/systemd/user/bureau-review-steward.service
install -Dm644 ops/systemd/bureau-review-steward.timer \
  ~/.config/systemd/user/bureau-review-steward.timer
systemctl --user daemon-reload
systemctl --user enable --now bureau-review-steward.timer
```

The timer runs hourly at minute 23, after Closure lane selection. Conservative classifications are
limited to `reviewing`, `needs_revision`, `ci_failed`, `merge_candidate`, `blocked` and `obsolete`.
A `merge_candidate` means only that the lane can be handed to the merge gatekeeper; it is not a
merge permission.

Manual checks:

```bash
bureau-review-steward run --max-lanes 4
systemctl --user status bureau-review-steward.timer
journalctl --user -u bureau-review-steward.service -n 50 --no-pager
```
