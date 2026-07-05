# Architecture

Bureau separates durable intent from volatile execution.

Git contains initiatives, tasks, resources, queue order and exact plan references. SQLite contains
workers, runs, reservations, revision-bound task overlays, receipts, workspace records and an
operational event stream. The database uses migrations, WAL, `synchronous=FULL`, foreign keys and
`BEGIN IMMEDIATE` for atomic dispatch.

A run freezes both `task_sha256` and `plan_sha256`. A receipt is valid only for those exact
revisions. Editing a previously completed task or its plan makes the operational overlay `stale`
and blocks dependants until the new revision is verified.

## Component model

Bureau is a coordination core with optional operational organs around it.

| Layer | Contents | Owner role |
|---|---|---|
| Registry | initiatives, tasks, resources, queue order, source snapshots | durable Bureau intent |
| Core runtime | claim, reconcile, checkout, envelope, workspace and receipt logic | Bureau task coordination |
| State store | SQLite runs, workers, reservations, overlays, receipts, workspaces and events | volatile execution state |
| Bureau Ops | closure observation, review stewardship, bridge helpers, frontier, discovery and cycle contracts | observation, derivation, registration and evidence support |
| External systems | GitHub, Grabowski, Steuerboard, Cabinet, Schauwerk and Chronik | their own source facts and actions |

The ops layer is named because the repository contains more than the dispatch kernel. Ops code
preserves the source of every fact it derives and routes Bureau effects through registry tasks,
claims, evidence or receipts.

## Core saga

Bureau and Grabowski form a saga rather than a shared transaction:

1. Bureau reconciles local and externally bound runs.
2. Bureau claims the task and semantic coordination resources.
3. Bureau writes an immutable execution envelope.
4. Bureau creates a baseline-bound workspace when required.
5. Grabowski acquires concrete leases and starts execution.
6. Bureau binds and observes the external identity.
7. Bureau verifies evidence and commits a receipt.

The adapter contract is deliberately small: `dispatch(request)` and `observe(external_id)`. An
unavailable adapter creates a visible reconcile finding; it never causes a bound run to be silently
forgotten.

## Owner matrix

| Concern | Primary owner | Bureau Core role | Bureau Ops role |
|---|---|---|---|
| Queue order and dependency unlocking | Bureau | Owns claims, stale overlays and receipts | May propose task changes |
| Concrete execution | Grabowski | Binds external identity | May request or observe dispatch |
| Pull requests, reviews and CI | GitHub | Stores Bureau evidence about tasks | May observe PR facts and derive findings |
| Readiness evidence | Steuerboard | Requires explicit evidence before unlock | May point to missing or stale evidence |
| Research and decisions | Cabinet | References decisions when tasks need them | May import bounded findings through a bridge |
| Visual projection | Schauwerk | No visual truth ownership | May expose Bureau facts for projection |
| Events | Chronik | May bind receipts to append-only events | May emit or consume event references |

## Rules for operational organs

1. Observation is not ownership: derived findings keep the source system visible.
2. Registration is explicit: new work enters Bureau through registry tasks and queue order.
3. Evidence is revision-bound: any Bureau effect is tied to task and plan revisions when it unlocks dependencies.
4. Failure is visible: if an external source cannot be observed, ops records a finding.
5. Package extraction is a later decision, after dependencies and deployment costs are known.

## Source inboxes

A source inbox is an observation layer, not a commitment layer. The Weltgewebe adapter resolves one
local Git ref to one exact commit and reads `docs/tasks/index.json` and its Draft-07 schema from that
same commit with fixed, non-networked Git commands. The resulting snapshot records source facts,
per-task hashes and evidence counts under `registry/sources/`.

Source snapshots never establish Bureau task materialisation, readiness, dependency completeness,
parallel write safety or autonomous execution permission. A future promotion step must make those
decisions explicitly and preserve Bureau-owned coordination state.
