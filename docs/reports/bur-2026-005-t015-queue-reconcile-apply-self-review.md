# BUR-2026-005-T015 self-review: queue reconcile reviewed apply path

Date: 2026-07-09
Task: BUR-2026-005-T015
Implementation diff SHA-256: a206aa546dc3c8bf9cffb5a4745d6491eab782174dddda531c2be14309b932c7
Scope: docs/operations.md, src/bureau/cli.py, src/bureau/queue_reconcile.py, tests/test_queue_reconcile.py

## Reviewed change

The patch adds a guarded queue-reconcile plan/apply path:

- `queue-reconcile --write-plan PATH` writes a dry-run-bound plan artifact.
- `queue-reconcile --apply-plan PATH` requires `review.status=reviewed`, `reviewer`, and `reviewed_at`.
- Apply checks queue hash and dry-run report hash before mutation.
- Apply recomputes expected queue from the reviewed actions and refuses mismatches.
- Apply runs post gates: check-equivalent state integrity, doctor, and registry-truth.
- Apply rolls `registry/queue.json` back if post gates fail.
- Apply does not claim, dispatch, complete, or merge work.

## Findings

### Blockers

None found in the reviewed implementation diff.

### Risks / trade-offs

- The apply surface is intentionally narrow: only deterministic add-to-`now` and add-to-`next` actions are applied. It does not yet remove blocked backlog tasks or mark planned tasks ready.
- The current live Bureau still has separate hygiene findings, such as blocked tasks in `later` and state-root `reviews/`. This patch gives Bureau a safe queue mutation mechanism but does not make every live doctor finding disappear.
- Plan review is file-based. It depends on a reviewer editing the plan artifact correctly; the tool enforces required review fields and dry-run parity but does not verify reviewer identity cryptographically.

## Validation

- `PYTHONPATH=src pytest -q tests/test_queue_reconcile.py tests/test_v2.py tests/test_registry_queue.py tests/test_bureau.py` passed.
- `PYTHONPATH=src ruff check src/bureau/queue_reconcile.py src/bureau/cli.py tests/test_queue_reconcile.py` passed.
- `PYTHONPATH=src pytest -q` passed.
- Smoke: `queue-reconcile --write-plan` produced five current safe add-to-next actions on live Bureau; `--apply-plan` refused the pending unreviewed plan and left `registry/queue.json` unchanged.

## Non-claims

This review does not establish semantic correctness of every future queue plan, suitability of every queued task, or merge readiness without CI.
