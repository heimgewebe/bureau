# Bureau Truth Model v2

## Purpose

Bureau coordinates commitments, resources, execution state, references, evidence, blockers and safe next work without replacing GitHub, Grabowski, Chronik or repository-local truth.

The v2 model separates durable constitutional truth from fast operational evidence and makes the repository frontier the primary derived work view.

## Truth layers

### 1. Git constitution

Reviewed Git remains authoritative for initiatives, task definitions, goals, acceptance criteria, dependencies, resource claims, safety and authority rules, and strategic priority.

### 2. Operational ledger

The state store may record focus changes, blockers and resolutions, run state, PR/CI/deploy/runtime evidence, bounded lifecycle proposals and drift findings. Ledger entries are atomic, source-bound and append-only where practical. They cannot verify a task without valid evidence and cannot replace an external authority.

### 3. Repository frontier

The derived frontier answers which safe ball is active or claimable per repository. It combines canonical queue priority with tasks, claims, leases, runs, open PRs and blockers. Live Register remains context only and never grants claim, dispatch, merge or deployment authority.

## Delivery slices

1. Make every Live Register operational projection complete regardless of displayed history length and expose coverage metadata.
2. Publish an independent read-only status capsule with explicit freshness and stale/unavailable states.
3. Detect registry/implementation drift and produce a reviewed closeout plan without blind verification.
4. Run lifecycle-ledger proposals in shadow mode and measure parity before reducing Git lifecycle updates.
5. Make the repository frontier the primary work view and expose parallel conflict-free repository balls.
6. Classify and revalidate long-lived backlog tasks rather than bulk-editing them.
7. Replace the initial complete event scan with an indexed or materialized projection only after correctness and parity are proven.
8. Reconcile orphaned workspace rows through a reviewed, evidence-bound path.
9. Align status-projection health with Doctor and Registry Truth semantics.
10. Revalidate stale Live Register candidates against current PR, registry and runtime evidence.

## Safety boundaries

This initiative does not add automatic deletion, dispatch, claim, merge, deploy or task-verification authority. Mutations remain behind existing Grabowski, review, CI, recovery and authorization gates. An incomplete conflict projection fails closed.

## Success measures

- Active focus remains visible after arbitrary unrelated history growth.
- Coverage, truncation, source and freshness are explicit.
- A read-only Bureau status remains available when the full Grabowski operator session is unavailable.
- Merged implementation and task lifecycle drift cannot remain silent.
- One write ball per repository is visible while different repositories can proceed in parallel.
- Git lifecycle reduction is considered only after shadow-mode parity and error-rate evidence.
