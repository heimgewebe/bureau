# Runtime Automation Contract

Status: active contract for Bureau runtime automation work
Owner layer: Bureau Core with Bureau Ops consumers
Source plan: `docs/plans/bureau-runtime-automation-baseline-v1.md`

Bureau automation is a control-tower contract, not an autopilot permission. Every automated loop must keep three questions separate:

1. Which source system produced the fact?
2. Which Bureau state may this loop mutate?
3. Which decision remains outside the loop's authority?

## Authority model

| Surface | Owns | May do | Must not do |
|---|---|---|---|
| Registry | initiatives, tasks, resources, queue order and durable plan references | define Bureau intent through reviewed Git changes | represent live runtime truth |
| State root | SQLite runs, reservations, task overlays, workspaces, events, envelopes and receipts | record operational truth and materialised evidence | overwrite source authority |
| Observer evidence | source-attributed facts from GitHub, Grabowski and other external systems | record observations with identifiers and timestamps | convert observations directly into completion |
| Status projection | read-only status board assembled from registry, state root and observations | explain current effective state and unknowns | mutate registry, runs, receipts or source systems |
| Dispatcher | optional claim and external dispatch entry point | claim only eligible ready work under explicit policy | merge, complete, clean up dirty workspaces or bypass evidence |
| Merge gate | explicit final merge decision | merge only under a separate gate policy | inherit authority from CI, observer or dispatcher success |

Bureau Core owns coordination and receipt validity. Bureau Ops may observe, derive, propose and materialise explicit evidence. External authorities keep their own facts: GitHub owns branches, pull requests, reviews and CI; Grabowski owns concrete processes, leases and durable workers.

## Scheduler contract

Every scheduled loop must also be callable as a bounded one-shot command. systemd user timers are the default local Linux deployment profile, not a Bureau Core dependency.

A scheduler loop must be:

- idempotent for repeated runs;
- bounded by timeout and explicit read/write paths;
- lock-aware, so overlapping runs do not corrupt the state root;
- fail-closed for ambiguous external observations;
- observable through logs, JSON output or persisted events;
- safe to stop without silently losing claimed work.

## Status vocabulary

The status board must preserve distinctions instead of flattening them into green/red.

| Status family | Examples | Meaning |
|---|---|---|
| Registry state | `planned`, `ready`, `blocked`, `verified`, `cancelled`, `superseded` | durable task declaration and eligibility input |
| Run state | `assigned`, `running`, `verifying`, `succeeded`, `failed`, `cancelled`, `orphaned` | operational run lifecycle in the state root |
| Observation state | `github-observation-blocked`, `pr-observed`, `ci-unknown`, `ci-pending`, `ci-passed`, `ci-failed`, `reviewing`, `changes-requested`, `approved`, `merged-observed` | source-attributed facts imported by observers |
| Freshness state | `current`, `stale`, `unknown` | whether task and plan hashes still match the verified evidence |
| Projection state | `eligible`, `blocked`, `needs-review`, `verification-needed`, `unknown` | read-only synthesis for operators and dashboards |

`merged-observed` is not `verified`. Green CI is not completion. External process success is not receipt evidence. A webhook event is not a state transition until a reconciler interprets it under this contract.

## Forbidden implicit powers

Baseline automation must not:

1. merge pull requests;
2. mark tasks complete from GitHub merge, approval or CI alone;
3. clean up dirty, unmerged or non-terminal workspaces;
4. dispatch non-ready tasks or bypass dependency checks;
5. treat webhook delivery as direct Bureau state mutation;
6. treat observer evidence as source authority;
7. mutate `current_plan.commit` or `document_sha256` without a freshness and re-verification strategy;
8. make systemd or any specific scheduler a required Bureau Core runtime.

Any exception requires a separate initiative or task with explicit acceptance criteria, source ownership and revalidation rules.

## Plan freshness rule

A run freezes both `task_sha256` and `plan_sha256`. Changing task or plan material after verification can stale existing receipts and block dependants. Plan pinning is therefore not harmless metadata; it is a freshness event and must be handled under the strategy task `BUR-2026-005-T008` before any pinning mutation is introduced.
