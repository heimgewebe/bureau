# Bureau Live Register v1 self-review

Date: 2026-07-10
Task binding: bootstrap implementation with explicit PR binding exception
Implementation diff SHA-256: 4c0bfc45aed9edf97f404e9ac6773053d77bc0881bd5c8f3f4c6021afea2d11f
Scope: state-store live-register events, CLI, docs, follow-up task registration, tests

## Reviewed change

This slice adds a gitless operational Live Register to Bureau without replacing the Git Registry:

- Adds `src/bureau/live_register.py` with append-only state-store events for `thread_focus`, `candidate_task` and `focus_override` records.
- Adds `bureau live-register` and `bureau live-list` CLI commands.
- Validates repo resources, task references, thread identifiers, status values and bounded text fields.
- Stores records in the existing SQLite `events` table with `event_type=live-register`.
- Adds summaries for active thread focus, active focus overrides, open candidates and promotion-required candidates.
- Documents the boundary in architecture, runtime contract, operations and `docs/bureau-live-register-v1.md`.
- Registers follow-up tasks `BUREAU-LIVE-REGISTER-V1-T002` through `BUREAU-LIVE-REGISTER-V1-T006` for what-now integration, repo-balls overlay, candidate promotion, thread/worker conflict view and retention/Chronik export.

## Findings

No blocker found in the implementation diff.

## Risks and boundaries

- Live Register is operational evidence only. It must not be treated as queue truth, registry task truth, claim authority, dispatch authority or merge readiness.
- The first slice records and lists live focus; it does not yet integrate with `what-now` or `repo-balls`.
- Candidate promotion remains a registered follow-up; this slice deliberately avoids automatic Registry PR generation from live events.
- Live-register events use the existing events table. Retention/export policy remains a registered follow-up.

## Validation

- `PYTHONPATH=src pytest -q tests/test_live_register.py` passed.
- `PYTHONPATH=src pytest -q tests/test_live_register.py tests/test_v2.py tests/test_registry_queue.py` passed.
- `PYTHONPATH=src pytest -q` passed.
- `PYTHONPATH=src ruff check src tests` passed.
- `git diff --check` passed.
- `PYTHONPATH=src bureau --root . --json check` reported valid=true.
- `PYTHONPATH=src bureau --root . --json doctor` reported healthy=true.
- `PYTHONPATH=src bureau --root . --json registry-truth` reported healthy=true.
- Smoke: `bureau live-register --kind thread_focus ...` wrote one state-store event and `bureau live-list --thread-id chat-smoke` listed it with active_thread_focus_count=1.

## Non-claims

This review does not establish that Live Register is already a full multi-thread scheduler, that it can promote candidates to durable tasks, that it changes queue policy, or that it replaces reviewed Git Registry PRs.
