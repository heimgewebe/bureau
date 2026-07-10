# Bureau Live Register evidence normalize v1 self-review

Date: 2026-07-10
Scope: BUREAU-LIVE-REGISTER-V1-T002..T006 verification metadata only

## Reviewed change

This registry-only patch changes the Live Register follow-up task verification source from
`local_worktree_pending_pr` to the merged GitHub PR #380 evidence identity.

No task definition, acceptance criterion, code, queue lane, initiative state or execution boundary is changed.

## Validation

- `PYTHONPATH=src bureau --root . --json check` reported valid=true.
- `PYTHONPATH=src bureau --root . --json doctor` reported healthy=true.
- `PYTHONPATH=src bureau --root . --json registry-truth` reported healthy=true.
- `git diff --check` passed.

## Non-claims

This review does not add new Live Register behavior and does not change queue, claim, dispatch or merge authority.
