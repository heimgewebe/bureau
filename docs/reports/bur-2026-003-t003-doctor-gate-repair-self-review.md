# BUR-2026-003-T003 self-review: doctor gate repair for queue apply

Date: 2026-07-09
Task: BUR-2026-003-T003
Implementation diff SHA-256: 5e005edca7d1e7310ce5f5cdb889dba334e73ae82410d55e7888b83a51471628
Scope: registry lifecycle states, registry/queue.json, state-root reviews classifier, state-root hygiene tests

## Reviewed change

This patch removes pre-existing Doctor blockers that prevented the reviewed queue-reconcile apply path from running after PR #344:

- Removes blocked tasks from the dispatch queue backlog: WELTGEWEBE-PUBLIC-LOGIN-OPS-V1-T001, UTIL-BASE-V1-T003 and UTIL-BASE-V1-T005.
- Aligns initiative lifecycle states with current lifecycle diagnostics:
  - SCHAUWERK-OPTIMIZATION-V1 -> completion-ready
  - SEMANTAH-SECURITY-HYGIENE-V1 -> active
  - UTIL-BASE-V1 -> waiting
- Classifies state-root reviews/ as a known review evidence directory instead of unknown state-root clutter.
- Adds a focused state-root hygiene regression test for reviews/.

## Findings

### Blockers

None found in the reviewed implementation diff.

### Risks / trade-offs

- Removing blocked tasks from registry/queue.json means they remain registered tasks but stop acting as dispatch backlog entries until their blockers clear.
- SCHAUWERK is marked completion-ready, not completed; this preserves a separate final completion decision.
- reviews/ is now treated as known active state-root evidence. This is correct for diff/self-review artifacts, but it does not validate the content quality of every review file.

## Validation

- PYTHONPATH=src bureau --root . --json check passed.
- PYTHONPATH=src bureau --root . --json doctor reported healthy=true, no queue findings, no unknown state-root entries and no lifecycle mismatches.
- PYTHONPATH=src pytest -q tests/test_state_root_hygiene.py tests/test_v2.py tests/test_registry_queue.py tests/test_queue_reconcile.py passed.
- PYTHONPATH=src ruff check src/bureau/v2.py tests/test_state_root_hygiene.py passed.
- PYTHONPATH=src pytest -q passed.
- git diff --check passed.

## Non-claims

This review does not establish that blocked utility or Weltgewebe work is complete, that Schauwerk is completed, or that the next queue plan should be merged without its own review.
