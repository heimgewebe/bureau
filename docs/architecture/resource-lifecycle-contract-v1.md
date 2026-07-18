# Cross-owner resource lifecycle contract v1

Bureau exposes a read-only lifecycle vocabulary for resources whose operational ownership spans
Bureau, Grabowski, Git, Chronik, RepoGround and product runtimes. The contract does not create a
cleanup service or claim live state. It defines the evidence that later owner-specific cleanup and
reconciliation must preserve.

## Invariant

Operational ownership ends only through authoritative terminal evidence. Expiry, age, process
absence, queue movement or a successful command alone do not establish terminality. Cleanup may
remove an active projection or derived payload, but it retains the immutable evidence needed to
explain and recover the effect.

The machine-readable surface is:

```bash
bureau --json resource-lifecycle-contract
bureau --json resource-lifecycle-contract --kind git-worktree
```

Its JSON Schema is `schemas/resource-lifecycle.v1.schema.json`.

## Resource classes

The initial contract covers task runs, coordination claims, Grabowski execution leases, Git
worktrees, workers, sensitive profiles, caches, durable outboxes, generated bundles, feature flags,
compatibility layers and deployment staging.

Each class declares:

- the authority and operational owner;
- accepted terminal evidence and explicitly forbidden inferences;
- retention for the active claim and historical evidence;
- cleanup trigger, effect and idempotency rule;
- orphan observation and fail-closed action;
- owners responsible for later migration work.

## Migration boundary

The contract is deliberately status-free. Owner repositories migrate incrementally:

1. inventory their existing mechanisms;
2. add read-only conformance reporting;
3. repair stale active projections;
4. bind exact cleanup to terminal evidence;
5. measure projection lag and orphan growth;
6. enable bounded automation only after negative controls pass.

Cross-owner wording does not transfer authority. Bureau remains authoritative for coordination
runs and claims; Grabowski remains authoritative for tasks, leases and workers; Git and each owning
repository remain authoritative for worktrees and releases; producers remain authoritative for
unacknowledged outbox entries.
