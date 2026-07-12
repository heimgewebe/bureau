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

## Completion review — 2026-07-12

A live acceptance review found that the 2026-07-09 implementation was not yet sufficient for task
closeout:

- the generated `registry.git_head` was recorded but not checked during apply;
- the report hash was checked, but reviewed `actions` were not compared with actions freshly
  derived from the current dry-run report; a coherently modified action/expected-queue pair could
  therefore remain self-consistent;
- the recorded registry root was not enforced;
- head drift during the validation/write window was not rechecked.

The completion patch now binds apply to the same registry root and non-empty Git head, compares the
reviewed action list exactly with current safe dry-run actions, rechecks the head before and after
the queue write and after post gates, and rolls the original queue bytes back on any post-write
failure. Regression tests cover missing or changed head, mismatched root, coherently tampered
actions, pre-effect head drift and post-write rollback. A live smoke additionally found that an
empty plan rewrote the pretty queue as compact JSON; the completion patch now returns a byte-stable
explicit no-op for empty actions and preserves readable two-space JSON for real updates.

The file-based review still does not cryptographically prove reviewer identity. That remains an
explicit non-claim; effect safety is established by deterministic action parity, root/head/queue
binding, post gates and rollback. The separate follow-up `BUR-2026-005-T019` records the remaining
auditability improvement: bind reviewed-plan evidence to the exact canonical plan payload digest
without pretending that the digest authenticates reviewer identity.
