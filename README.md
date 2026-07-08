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
available. Queue reads are advisory only; only `claim-next` and `checkout-next` reserve work
before any workspace, branch or PR is created. Open PRs are treated as external reservations,
with same-task PRs reported as duplicate implementations and other open PRs as conservative
repo-write blockers. Open-PR observation uses `BUREAU_OPEN_PR_CLAIM_GUARD_LIMIT`
(default 500); if the observed page reaches that cap, coverage is explicitly bounded and
that repository fails closed instead of being treated as completely observed.

## Boundaries

Bureau is documented as a core plus operational organs.

- **Bureau Core** owns commitments, ordering, dependencies, coordination claims, dispatch,
  completion verification, immutable envelopes and revision-bound receipts.
- **Bureau Ops** observes adjacent systems, derives findings, proposes work and materialises
  explicit evidence through Bureau Core contracts. Ops findings are evidence for Bureau Core to
  consider, not permission to replace the authority that produced the fact.
- **External authorities** retain their domains: GitHub owns branches, pull requests, reviews and
  CI facts; Grabowski owns processes, hosts, concrete runtime leases, durable tasks and workers;
  Steuerboard owns action-specific readiness; Cabinet owns readable research and decisions;
  Schauwerk owns visual projections; Chronik owns append-only events.

Bureau does not implement another shell, general workflow engine, knowledge base or project UI.
Ops organs keep source ownership visible and bind Bureau effects to explicit tasks, claims,
evidence or receipts.

## Operational organs

Optional ops organs in this repository include closure observation, review stewardship, Codex
bridging, agent frontier, source discovery, Cabinet bridges and cycle contracts. They surround the
core; they are not replacement authorities for their source systems.

Runtime automation follows the contract in `docs/runtime-automation-contract.md`: scheduler loops
remain one-shot callable, observer facts stay source-attributed, status projection is read-only,
and merge, cleanup and completion authority are not implicit automation powers.

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
- immutable execution envelopes and evidence-complete receipts;
- optional ops organs for source, closure, review, bridge, frontier, Cabinet and cycle observation.

## Safety invariants

1. A receipt verifies exactly one task hash and one plan hash.
2. A changed task or plan becomes `stale`; old evidence never unlocks dependencies.
3. Reconciliation never silently ignores an unobservable external executor.
4. Worktrees are removed only after terminal runs and never silently discard dirty work.
5. Process success is not completion; every acceptance criterion still needs evidence.
6. Ops observations never replace the authority of their source systems.

### Source promotion preview

Plan a single source-task promotion without writing Registry tasks:

```bash
bureau --root . --json source-promote-plan weltgewebe --task-id DEPLOY-DNS-001
```

The command returns the would-be Bureau ID, the source commit and task hash, unresolved manual
decisions and a non-executed candidate task. It does not create a task, touch the queue or grant
readiness.
