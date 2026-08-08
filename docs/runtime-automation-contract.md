# Runtime Automation Contract

Status: active operational contract for Bureau runtime automation work
Owner layer: Bureau Core with Bureau Ops consumers
Source plan: `docs/plans/bureau-runtime-automation-baseline-v1.md`
Historical baseline evidence: `docs/bureau-runtime-automation-contract-v1.md`
Historical task binding: `registry/tasks/BUR-2026-005-T001.json`

Bureau automation is a control-tower contract, not an autopilot permission. Every automated loop must keep three questions separate:

1. Which source system produced the fact?
2. Which Bureau state may this loop mutate?
3. Which decision remains outside the loop's authority?

## Document lineage and authority

`docs/bureau-runtime-automation-contract-v1.md` is the retained acceptance artifact for the verified historical task `BUR-2026-005-T001`. Its anchors are referenced directly from that task's Registry metadata, so it remains historical evidence and is not a second current contract to keep in sync by hand.

This file is the active operational delta. It does not maintain a second exhaustive authority matrix or status vocabulary. Current durable task facts come from the applicable `registry/tasks/*.json` record and queue placement from `registry/queue.json`; current run facts come from the Bureau StateStore and read-only CLI projections. `docs/architecture.md` describes the current component ownership model. When prose and a primary source disagree, the primary source wins and the prose is stale.

The baseline control-tower ownership and forbidden-power rationale remain documented in the historical v1 contract. The rules below contain only operational additions that still need an active home.

## Scheduler contract

Every scheduled loop must also be callable as a bounded one-shot command. systemd user timers are the default local Linux deployment profile, not a Bureau Core dependency.

A scheduler loop must be:

- idempotent for repeated runs;
- bounded by timeout and explicit read/write paths;
- lock-aware, so overlapping runs do not corrupt the state root;
- fail-closed for ambiguous external observations;
- observable through logs, JSON output or persisted events;
- safe to stop without silently losing claimed work.

## State-source invariants

Do not copy mutable runtime enumerations into this document. Read them from the current Registry and StateStore projections instead. The cross-source invariants that remain stable are:

- Registry task state and queue placement are durable Bureau intent, not GitHub or CI facts.
- StateStore run state is operational truth for one bound run, not permanent task verification.
- GitHub owns observed pull-request, review and merge facts; CI owns only the result for its exact run and commit.
- `merged-observed` is not `verified`; green CI is not completion.
- An external process success is not Bureau receipt evidence.
- A webhook event is an observation until an explicit reconciler interprets it under a mutation contract.

## Active authority deltas

The historical v1 contract already forbids implicit merge, cleanup, task verification, initiative completion, queue mutation, unsafe dispatch and deploy authority. This active contract adds four operational restrictions that must not be inferred away:

1. webhook delivery does not directly mutate Bureau lifecycle state;
2. dispatcher or scheduler success does not authorize merge, completion or cleanup;
3. non-ready work and dependency checks cannot be bypassed by a scheduler;
4. changing `current_plan.commit`, `document_sha256` or other plan identity requires an explicit freshness and re-verification strategy.

Any exception requires a separate initiative or task with explicit acceptance criteria, source ownership and revalidation rules.

## Plan freshness rule

A run freezes both `task_sha256` and `plan_sha256`. Changing task or plan material after verification can stale existing receipts and block dependants. Plan pinning is therefore not harmless metadata; it is a freshness event and must be handled under the strategy task `BUR-2026-005-T008` before any pinning mutation is introduced.
