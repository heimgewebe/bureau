# BUR-2026-003-T007 self-review: minimal what-now

Task: BUR-2026-003-T007
Branch: feat/minimal-what-now-v1
Base: origin/main
Diff SHA-256: 489c0080288a75dd83db8144e35ba86ff5ae36c6cd26571c4eb772ebce3ad4c4

## Scope reviewed

- Adds `bureau what-now` as a read-only CLI command.
- Adds `bureau.what_now.what_now_report`.
- Adds regression tests for eligible ranking, no-eligible blocker visibility, and CLI JSON output.

## Acceptance mapping

- eligible-ranking: covered by `test_what_now_ranks_eligible_tasks_from_registry_truth`.
- blockers-visible: covered by `test_what_now_exposes_runtime_truth_and_blockers_when_none_eligible`.
- no-chat-memory: implementation reads from Dispatcher frontier, registry tasks/queue, state store, lifecycle diagnostics, and resource reservations only.

## Findings

- No mutation path is introduced. `what-now` returns `read_only: true` and does not call claim, checkout, queue reconcile apply, or workspace mutation.
- The rank basis is explicit: state, queue lane/index, priority lane/rank, dependencies, and resource claims.
- Existing Dispatcher blocker logic remains authoritative; the new command only repackages and ranks it.

## Risks / trade-offs

- It ranks the visible frontier after applying the requested `--limit`; if many eligible tasks exist beyond the limit, they are intentionally omitted from the compact report.
- Open PR observation remains inherited from Dispatcher behavior, so `what-now` can take network/GitHub time like `explain-next`.

## Validation

- `PYTHONPATH=src python3 -m ruff check src/bureau/what_now.py tests/test_what_now.py src/bureau/cli.py`
- `PYTHONPATH=src python3 -m pytest tests/test_what_now.py tests/test_bureau.py::test_registry_loads tests/test_registry_queue.py::test_registry_queue_does_not_contain_terminal_tasks`
- `PYTHONPATH=src python3 -m bureau.cli --json check`
- `PYTHONPATH=src python3 -m bureau.cli --json registry-truth --no-baseline-probe`
- `PYTHONPATH=src python3 -m bureau.cli --json what-now --capability repository --capability shell --capability grabowski --limit 5`
- `PYTHONPATH=src python3 -m pytest` → 453 passed
