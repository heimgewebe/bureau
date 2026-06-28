# Architecture

Bureau separates durable intent from volatile execution.

Git contains initiatives, tasks, resources, queue order and exact plan references. SQLite contains
workers, runs, reservations, revision-bound task overlays, receipts, workspace records and an
operational event stream. The database uses migrations, WAL, `synchronous=FULL`, foreign keys and
`BEGIN IMMEDIATE` for atomic dispatch.

A run freezes both `task_sha256` and `plan_sha256`. A receipt is valid only for those exact
revisions. Editing a previously completed task or its plan makes the operational overlay `stale`
and blocks dependants until the new revision is verified.

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
