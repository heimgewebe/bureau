# Bureau Runtime Observation & Projection v1

Status: closes `BUR-2026-005-T002`, `BUR-2026-005-T003`, `BUR-2026-005-T004`.
Contract: `docs/bureau-runtime-automation-contract-v1.md`
Plan: `docs/plans/bureau-runtime-automation-baseline-v1.md`

Bureau gains eyes and a dashboard here, not an autopilot. Everything in this
document observes and projects; nothing decides, merges, completes, mutates
the queue or cleans up.

## Scheduler contract (T002)

Every scheduled loop is an idempotent one-shot command. Each command can run
manually, under systemd, cron or any other scheduler, with identical
semantics. systemd is the local Linux **reference deployment**, not a Bureau
Core dependency: Bureau never requires a running timer to stay correct, and
manual one-shot invocation must always remain possible.

### One-shot commands

| Command | Reads | Writes | Purpose |
|---|---|---|---|
| `bureau --root R --json check` | registry, state DB (ro) | nothing | registry validity + state integrity |
| `bureau --root R --json status` | registry, state DB | state DB (schema init only) | summary, runs, lifecycle |
| `bureau --root R --json doctor` | registry, state DB | state DB (schema init; repairs only with `--repair`) | composite diagnosis |
| `bureau --root R --json runtime-drift-check` | registry, state DB (ro), git (`--no-optional-locks`) | nothing | drift findings |
| `bureau --root R --json reconcile --stale-after 900` | registry, state DB, adapters | state DB (bounded, see below) | bounded reconcile loop |
| `bureau --root R --json github-observe` | registry, state DB (ro), `gh` | nothing | PR/CI/review evidence |
| `bureau --root R --json status-projection` | registry, state DB (ro), `gh` | nothing | per-task status board |

`status-projection` accepts `--skip-github` (no observation, reported as
unknown), `--github-observations FILE` (use stored observer output instead of
calling `gh`), `--github-max-age SECONDS` (staleness threshold, default 3600)
and `--repo OWNER/NAME`. `github-observe` accepts `--repo` and `--task-id`
and exits non-zero when the observation is blocked, so a scheduler surfaces a
broken `gh` session as a failed unit instead of silent staleness.

### What reconcile may mutate

`reconcile` is the one scheduled command with write access, and its writes are
bounded to the Bureau runtime overlay in the state store:

- mark heartbeat-stale runs without an external executor as `orphaned` and
  release their reservations;
- reconstruct missing run envelope files from the stored envelope JSON;
- refresh `external_state`/`external_observed_at` for runs bound to an
  external adapter, moving a run to `verifying` at most;
- append observation events.

It never dispatches work, never claims tasks, never verifies or completes
anything, never touches `registry/` or the queue, never merges and never
deletes branches, worktrees or workspaces. An unavailable adapter is reported
as `unobserved`, never treated as success. These mutations change runtime
overlays: a run that looked `running` may become `orphaned` or `verifying`
after a reconcile tick, and the status projection will show the new overlay.

### Paths, locks, timeouts

- State root: `$BUREAU_STATE_DIR` (default `~/.local/state/bureau`), database
  `bureau.sqlite3`, run envelopes and receipts as materialized files beside it.
  `--state-root`/`--state-db` override per invocation.
- Locking: writers use SQLite `BEGIN IMMEDIATE` transactions on the state
  database; concurrent one-shot invocations serialize on that lock. Observers
  (`check`, `runtime-drift-check`, `github-observe`, `status-projection`)
  open the database read-only (`mode=ro`) and never take write locks; git
  reads use `--no-optional-locks`.
- Timeouts: `gh` calls time out after 30s and git remote resolution after 10s
  inside the process; the reference units add `TimeoutStartSec` (3min
  projection, 5min reconcile) as the outer bound.
- Idempotency: running any command twice in a row without external change
  yields the same result; `reconcile` converges (a second run finds nothing
  left to orphan or refresh).

### Failure semantics

- `gh` missing, failing or returning invalid JSON → the observation is
  `blocked` with a reason; nothing fails open, `github-observe` exits 1.
- Ambiguous PR binding → `ambiguous`, fail-closed, never success.
- State store missing → observers report `runtime-state-unavailable` and keep
  going; they do not create the database.
- Scheduler missing or disabled → freshness degrades visibly (older
  `observed_at`/`generated_at` timestamps); manual one-shot invocation remains
  the fallback.
- A failed unit is visible via `systemctl --user list-units --failed` and the
  journal; the commands print their JSON result to stdout, so the journal
  holds the full evidence of every tick.

## GitHub observer (T003)

`bureau --root R --json github-observe` imports open-PR facts as
source-attributed evidence: PR state, draft state, head ref/SHA, base ref,
merge state, review decision, per-check states with a summary
(`ci_unknown` / `ci_pending` / `ci_failed` / `ci_passed`) and an observation
timestamp. GitHub keeps authority over all of it.

Binding order (explicit markers beat heuristics):

| Rank | Binding | Confidence |
|---|---|---|
| 1 | `Bureau-Run: <run-id>` marker in PR title/body | 1.00 |
| 2 | `Bureau-Task: <task-id>` marker in PR title/body | 0.95 |
| 3 | branch-name fallback (task id in head ref, or recorded run workspace branch) | 0.55 |
| 4 | no match | unmatched |
| 5 | conflicting markers, several task candidates, several open PRs for one task | ambiguous, fail-closed |

`CHANGES_REQUESTED` marks the observation review-blocked regardless of other
approvals; a draft PR is never presented as merge-ready; a passing check
proves only the listed jobs on the observed head.

## Status projection board (T004)

`bureau --root R --json status-projection` emits one stable JSON document
(`schema_version: 1`) with, per task: registry state, effective state
(runtime overlay), queue lane, active run and worker, workspace, receipts,
bound GitHub evidence with confidence, plus explicit `findings`, `unknowns`,
`stale_reasons` and `blocked_reasons`. Top-level `healthy` is false whenever
a hard blocked/stale/ambiguous condition exists; unknowns stay visible
without flipping health. A receipt is run evidence, not completion; a merged
PR without current Bureau verification stays non-verified and is reported as
a finding; a task whose declared priority lane is missing from
`registry/queue.json` is reported (`declared-lane-not-queued`), not repaired.

The projection ends with a `does_not_establish` list: task completion, merge
readiness, CI sufficiency, runtime correctness, security correctness, and
merge/completion/dispatcher authority are all explicitly out of scope.

## Reference deployment (systemd user units)

`ops/systemd/` provides hardened user-level one-shot units as the local Linux
reference: `bureau-status-projection.{service,timer}` (read-only board, twice
hourly) and `bureau-reconcile.{service,timer}` (bounded reconcile loop,
hourly, write access restricted to `~/.local/state/bureau` via
`ReadWritePaths`). Static tests (`tests/test_runtime_observation_systemd.py`)
pin both units to non-dispatching subcommands.

The units expect a deployment venv at `~/.local/share/bureau/venv` with the
Bureau checkout installed and a `gh` session for the projection unit.

Install and enable:

```bash
python3 -m venv ~/.local/share/bureau/venv
~/.local/share/bureau/venv/bin/pip install -e ~/repos/bureau
install -Dm644 ops/systemd/bureau-status-projection.service \
  ~/.config/systemd/user/bureau-status-projection.service
install -Dm644 ops/systemd/bureau-status-projection.timer \
  ~/.config/systemd/user/bureau-status-projection.timer
install -Dm644 ops/systemd/bureau-reconcile.service \
  ~/.config/systemd/user/bureau-reconcile.service
install -Dm644 ops/systemd/bureau-reconcile.timer \
  ~/.config/systemd/user/bureau-reconcile.timer
systemctl --user daemon-reload
systemctl --user enable --now bureau-status-projection.timer bureau-reconcile.timer
```

Status and journal inspection:

```bash
systemctl --user list-timers 'bureau-*'
systemctl --user status bureau-status-projection.service
journalctl --user -u bureau-status-projection.service -n 1 -o cat
journalctl --user -u bureau-reconcile.service --since -2h
```

Disable and rollback:

```bash
systemctl --user disable --now bureau-status-projection.timer bureau-reconcile.timer
rm ~/.config/systemd/user/bureau-status-projection.{service,timer} \
   ~/.config/systemd/user/bureau-reconcile.{service,timer}
systemctl --user daemon-reload
```

Removing the units removes nothing but scheduled freshness: every loop stays
available as a manual one-shot command.

## Does not establish

This slice does not establish task completion, merge readiness, CI
sufficiency, runtime correctness, security correctness, automatic merge
authority, automatic completion authority, dispatcher authority, queue
mutation authority or cleanup authority. Observation is not authority.
