# Architecture

Bureau separates durable intent from volatile execution.

Git contains initiatives, tasks, resources and queue order. SQLite contains workers, runs,
reservations, task overlays and receipts. The database uses WAL, `synchronous=FULL`, foreign keys
and `BEGIN IMMEDIATE` for atomic dispatch.

Bureau and Grabowski form a saga rather than a shared transaction:

1. Bureau claims the task and coordination resources.
2. Bureau writes an immutable execution envelope.
3. Grabowski acquires concrete runtime leases and starts execution.
4. Bureau binds the external task identity.
5. Reconciliation repairs interruption between stages.

Grabowski should persist an idempotent `request_id` and `origin_ref` so the dispatch crash window
can be reconstructed without duplicate execution.
