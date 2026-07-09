# BUR-2026-003-T004 self-review: approval path

Date: 2026-07-09
Task: BUR-2026-003-T004
Implementation diff SHA-256: 3c9c043bc5b5e67b142254a19a52fa511a4d065369a63e9c6bc4685cc600e5bf
Review-blocker fix patch SHA-256: 6bafcbaeb1488fb80f136441e251daa975d6b9e13d6e3b7779a49dda79e61533
Scope: approval classes, fail-closed approval evaluator, schema declaration, dispatch/import/queue integration, tests and documentation

## Reviewed change

This patch adds a deterministic Bureau approval core and wires it into the existing effect boundaries:

- Defines approval classes and levels in `src/bureau/approval.py` and `docs/bureau-approval-path-v1.md`.
- Adds a task execution schema declaration for `execution.approval`.
- Adds approval decisions to frontier output without making missing approval a task-selection blocker.
- Records explicit operator approval for `checkout-next` workspace/repository mutation before worktree creation.
- Records explicit operator approval for `checkout-next --dispatch` agent dispatch.
- Records explicit operator approval for Cabinet proposal previews created with `--approve`.
- Requires reviewed-plan approval evidence for queue-reconcile apply.
- Requires reviewed-receipt approval evidence for reviewed Cabinet Frontier imports when `--apply` writes a registry task.

## Findings

### Blockers

The follow-up review found one blocker: `reviewed_plan` and `reviewed_receipt` had the same numeric approval rank and could satisfy each other's typed gates. The fix replaces pure rank comparison with explicit `allowed_levels` per action class and adds negative regression tests for both substitutions.

### Risks / trade-offs

- The approval contract is intentionally conservative and fails closed for unknown action classes. This can block future effectful paths until they are classified.
- Frontier task output now includes `approval_contract` metadata. This is useful for visibility but should not be treated as execution approval by itself.
- Existing preview commands still require their prior explicit `--approve` flag. The new approval object records the gate; it does not broaden authority.

## Validation

- `PYTHONPATH=src python -m pytest tests/test_approval.py tests/test_v2.py::test_checkout_next_records_repository_mutation_approval` passed: 9 tests.
- `PYTHONPATH=src python -m pytest tests/test_approval.py tests/test_cabinet_bridge_preview.py tests/test_cabinet_frontier_reader.py tests/test_queue_reconcile.py tests/test_v2.py` passed: 109 tests.
- `PYTHONPATH=src python -m pytest tests/test_v2.py::test_checkout_next_records_repository_mutation_approval tests/test_v2.py::test_grabowski_task_handoff_uses_execution_resource_keys tests/test_v2.py::test_dispatch_response_loss_recovers_binding tests/test_v2.py::test_checkout_existing_binding_does_not_redispatch` passed.
- `PYTHONPATH=src python -m pytest` passed: 445 tests.
- `PYTHONPATH=src ruff check src tests` passed.
- `PYTHONPATH=src python -m bureau.cli --root . check` passed.
- `PYTHONPATH=src python -m bureau.cli --root . --json doctor` reported `healthy=true`.

## Non-claims

This review does not establish automatic merge authority, automatic runtime repair, automatic queue repair, automatic task verification for future work, CI sufficiency beyond the executed commands, or that all future effectful paths are already wired to the approval helper. The final review also caught and fixed a missing repository-mutation approval record before workspace creation, the follow-up review fixed typed approval substitution, and runtime mutation wiring remains registered as follow-up `BUR-2026-003-T009`.
