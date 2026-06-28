# Bureau

Bureau is the deterministic coordination and dispatch layer between plans and real work.

Its core interaction is:

```text
Look into Bureau and execute the next task.
```

`checkout-next` first reconciles earlier runs, computes the executable frontier, atomically claims
one compatible task, freezes a revision-bound execution envelope, creates an isolated workspace
when required and returns or dispatches the executor handoff. A second session therefore receives
a different non-conflicting task or an explicit explanation that no further safe parallel work is
available.

## Boundaries

- **Bureau** owns commitments, ordering, dependencies, coordination claims, dispatch and completion.
- **Grabowski** owns processes, hosts, concrete runtime leases, durable tasks and workers.
- **Steuerboard** owns action-specific readiness and specialised evidence.
- **Cabinet** owns readable research and decisions.
- **Schauwerk** owns visual projections.
- **Chronik** owns append-only events.

Bureau does not implement another shell, general workflow engine, knowledge base or project UI.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
make validate

bureau --root . explain-next --capability repository --capability shell
bureau --root . checkout-next --worker chatgpt-session-1 \
  --capability repository --capability shell
```

For a `grabowski-task` task, the local Grabowski adapter can dispatch immediately:

```bash
bureau --root . --grabowski-source ~/repos/grabowski/src \
  checkout-next --worker durable-1 --capability repository --capability shell --dispatch
```

Weltgewebe can be observed through a commit-bound source inbox without creating executable Bureau tasks:

```bash
bureau --root . --json source-check weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main
bureau --root . --json source-sync weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main
bureau --root . --json source-sync weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main --apply
```

`source-check` and preview-only `source-sync` are strictly read-only and do not initialise the
operational state store. `--apply` atomically updates only
`registry/sources/weltgewebe.json`. It does not create tasks, readiness, claims or execution
permission; promotion into Bureau commitments is a separate explicit operation.

Operational state is outside Git at `~/.local/state/bureau`. The database, envelopes and receipts
always derive from the same state root. Override it with `BUREAU_STATE_DIR`, `--state-root`, or
`--state-db`.

## Implemented in v0.2

- enforced Draft 2020-12 JSON contracts plus semantic validation;
- SQLite schema migrations, WAL and `synchronous=FULL`;
- atomic `claim-next`, one active run per task/worker and concurrent claim stress coverage;
- task and plan revision hashes on runs, receipts and operational task status;
- stale evidence detection after task or plan changes;
- idempotent DB-canonical completion and deterministic receipt materialisation;
- automatic reconciliation before checkout;
- observable external bindings through a typed adapter contract;
- closed Bureau-side `checkout-next` with optional Grabowski dispatch;
- hierarchical read/write/exclusive/capacity claims and dynamic scope expansion;
- baseline-bound Git worktrees with inspect, preserve and cleanup lifecycle;
- initiative lifecycle diagnostics, `explain-next`, and `doctor`;
- immutable execution envelopes and evidence-complete receipts.

## Safety invariants

1. A receipt verifies exactly one task hash and one plan hash.
2. A changed task or plan becomes `stale`; old evidence never unlocks dependencies.
3. Reconciliation never silently ignores an unobservable external executor.
4. Worktrees are removed only after terminal runs and never silently discard dirty work.
5. Process success is not completion; every acceptance criterion still needs evidence.
