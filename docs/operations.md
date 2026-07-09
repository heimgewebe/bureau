# Operations

## Diagnose

```bash
bureau --root . doctor --json
bureau --root . lifecycle --json
bureau --root . explain-next --capability repository --capability shell --json
```

`doctor` includes a read-only `state_root_hygiene` section. It treats only the configured Bureau database, SQLite sidecars, `envelopes/` and `receipts/` as known state-root artefacts. Unknown files or directories are reported, not deleted, including when `--repair` is used. Move or quarantine such files manually after checking whether they are operator notes, old backups or unrelated prompts.



## Queue freshness reconcile

`registry/queue.json` is the dispatch canon. `task.priority` is advisory metadata. Use
`queue-reconcile` to compare the two without mutating queue state:

```bash
bureau --root . --json queue-reconcile
bureau --root . --json queue-reconcile --resource repo.bureau
```

The report can identify ready priority-now tasks that are not queued, priority-next tasks absent
from queue, later-lane tasks whose advisory priority says now/next, terminal tasks still queued,
and now-lane tasks that are not ready. The default command is read-only: it does not promote
lanes, claim work, write tasks or close anything.

A queue mutation must go through a reviewed plan artifact. First write a plan:

```bash
bureau --root . --json queue-reconcile --write-plan /tmp/bureau-queue-plan.json
```

Review the plan's `actions` and `expected_queue_after`. To apply, edit `review.status` to
`reviewed` and add `reviewer` plus `reviewed_at`, then run:

```bash
bureau --root . --json queue-reconcile --apply-plan /tmp/bureau-queue-plan.json
```

Apply is deliberately narrow. It only applies deterministic add-to-`now`/add-to-`next` actions
from the reviewed plan, refuses if the queue or dry-run findings changed since the plan was
generated, runs post-apply registry/doctor/registry-truth gates, and rolls the queue back if a
post-apply gate fails. It never claims, dispatches, completes or merges work.

## Worktree hygiene

Use `worktree-hygiene` to inspect local Bureau worktree sprawl without deleting anything:

```bash
bureau --root . --json worktree-hygiene
bureau --root . --json worktree-hygiene --max-count 40
```

The report identifies detached worktrees, dirty worktrees, many-worktree pressure and heads already
merged into the current checkout head. These are cleanup candidates only after human or operator
review. The command never removes a worktree or branch.

## Console script packaging

Packaged console scripts are declared in `pyproject.toml`. If a command exists in pyproject but is
missing from the local shell, refresh the editable install:

```bash
python3 -m pip install -e '.[dev]'
```

Module entrypoints remain available through `python3 -m bureau.<module>` even when the shell wrapper
is stale.

## Repository-scoped balls

Bureau can project one current ball per repository resource without changing queue state:

```bash
bureau --root . --json repo-balls --capability repository --capability shell
```

Use a resource filter when asking for the next task for one repository:

```bash
bureau --root . --json explain-next --resource repo.bureau \
  --capability repository --capability shell
bureau --root . --json claim-next --worker worker-repo-bureau \
  --resource repo.bureau --capability repository --capability shell
```

A resource-scoped ball does not create a second queue canon. `registry/queue.json` remains the
dispatch queue. The resource filter limits observation, explanation and selection to tasks whose
claims overlap the requested resource.

Worker ownership is still one active assignment per worker ID. Use stable resource-scoped worker
IDs, such as `worker-repo-bureau` or `worker-repo-lenskit`, when operating multiple repository
balls in parallel. Reusing a worker ID for a different resource is rejected instead of silently
claiming another task.

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

## Closure pull-request observation

Closure may observe open GitHub pull requests when the repository origin resolves to a GitHub `owner/repo` slug and `gh pr list` is available. The observation is fail-soft, but not fail-open: if GitHub metadata cannot be read, Closure records a blocked GitHub observation. Existing PR-linked lanes keep their previous PR evidence and are blocked from closure decisions until observation succeeds again.

Open pull requests are recorded as coordination evidence, not as a second pull-request authority. GitHub remains the owner for pull-request state, checks, review decisions and mergeability. Closure only stores `pr`, `pr_title`, `pr_url` and `observed_github_state` on the lane so that existing work can be routed to the right closure path.

Conservative lane derivation from observed GitHub state is intentionally narrow:

- `DIRTY` becomes `needs_revision`.
- `UNSTABLE` or `UNKNOWN` becomes `ci_failed`.
- `CHANGES_REQUESTED` becomes `needs_revision`.
- Draft pull requests become `reviewing`.
- `CLEAN` plus `APPROVED` becomes `merge_candidate`.
- `CLEAN` without approval remains `reviewing`.

Existing `paused` lanes keep their operator hold when pull-request observation would otherwise derive a review, revision, or merge-candidate state.

A `merge_candidate` lane is only eligible for merge-gatekeeper handoff. It is not a merge permission and does not replace explicit checks, review-thread inspection or final merge policy.

## Runtime observation and status projection

The GitHub observer and the read-only status projection board, including
their scheduler contract and the `bureau-status-projection` and
`bureau-reconcile` reference timers under `ops/systemd/`, are documented in
`docs/bureau-runtime-observation-v1.md`. Quick start:

```bash
bureau --root . --json github-observe
bureau --root . --json status-projection
```

Both commands observe and project only. They never verify tasks, mutate the
queue, merge, delete branches or clean up worktrees.
