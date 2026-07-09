# BUR-2026-003-T004 self-review: approval path

Date: 2026-07-09
Task: BUR-2026-003-T004
Implementation diff SHA-256: 3c9c043bc5b5e67b142254a19a52fa511a4d065369a63e9c6bc4685cc600e5bf
Scope: approval classes, fail-closed approval evaluator, schema declaration, dispatch/import/queue integration, tests and documentation

## Reviewed change

This patch adds a deterministic Bureau approval core and wires it into the existing effect boundaries:

- Defines approval classes and levels in `src/bureau/approval.py` and `docs/bureau-approval-path-v1.md`.
- Adds a task execution schema declaration for `execution.approval`.
- Adds approval decisions to frontier output without making missing approval a task-selection blocker.
- Records explicit operator approval for `checkout-next --dispatch` agent dispatch.
- Records explicit operator approval for Cabinet proposal previews created with `--approve`.
- Requires reviewed-plan approval evidence for queue-reconcile apply.
- Requires reviewed-receipt approval evidence for reviewed Cabinet Frontier imports when `--apply` writes a registry task.

## Findings

### Blockers

None found in the reviewed implementation diff.

### Risks / trade-offs

- The approval contract is intentionally conservative and fails closed for unknown action classes. This can block future effectful paths until they are classified.
- Frontier task output now includes `approval_contract` metadata. This is useful for visibility but should not be treated as execution approval by itself.
- Existing preview commands still require their prior explicit `--approve` flag. The new approval object records the gate; it does not broaden authority.

## Validation

- `PYTHONPATH=src python -m pytest tests/test_approval.py tests/test_cabinet_bridge_preview.py tests/test_cabinet_frontier_reader.py tests/test_queue_reconcile.py tests/test_v2.py` passed: 105 tests.
- `PYTHONPATH=src python -m pytest` passed: 441 tests.
- `PYTHONPATH=src ruff check src tests` passed.
- `PYTHONPATH=src python -m bureau.cli --root . check` passed.
- `PYTHONPATH=src python -m bureau.cli --root . --json doctor` reported `healthy=true`.

## Non-claims

This review does not establish automatic merge authority, automatic runtime repair, automatic queue repair, automatic task verification for future work, CI sufficiency beyond the executed commands, or that all future effectful paths are already wired to the approval helper.
