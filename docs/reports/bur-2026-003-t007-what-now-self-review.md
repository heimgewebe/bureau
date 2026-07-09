# BUR-2026-003-T007 self-review: minimal what-now

Date: 2026-07-09
Task: BUR-2026-003-T007
Scope: read-only `bureau what-now` command, deterministic operator ranking, blocker visibility, tests and documentation
Implementation diff SHA-256: 8cc4405414913dcc6f66741fcbed69a35cc10b2e4e58b4dcdedbce12f7653cd5

## Reviewed change

This patch adds a compact read-only `what-now` path on top of Bureau registry/runtime truth:

- Adds `Dispatcher.what_now(...)` with ranked operator-eligible work, hard blockers and runtime truth.
- Adds `bureau what-now` CLI flags: `--capability`, `--resource`, `--limit`.
- Separates operator eligibility from strict claim eligibility via `eligible` and `claim_eligible`.
- Treats planned `interactive-agent/review-before-effect` tasks as operator-eligible soft-gated work, not autonomous claims.
- Preserves hard blocker reasons separately in `blocker_reasons` and aggregate `blocker_summary`.
- Documents the ranking and read-only boundary.

## Findings

### Blockers

None found in the reviewed implementation diff.

### Risks / trade-offs

- `what-now` intentionally returns operator-eligible tasks that `claim-next` would not claim. This is useful for review-before-effect workflows but must not be treated as dispatch authority.
- `runtime_truth.next_task_available` still reflects strict claimability. It can be false while `selected` is non-null because `selected` may require operator review.
- The command uses configured observations such as open PR guards, so observation outages can still create hard blocker reasons rather than silent eligibility.

## Validation

- `PYTHONPATH=$PWD/src python -m pytest tests/test_v2.py::test_what_now_ranks_eligible_tasks_from_registry_truth tests/test_v2.py::test_what_now_explains_blockers_when_no_task_is_eligible tests/test_v2.py::test_what_now_treats_planned_review_before_effect_as_operator_eligible tests/test_v2.py::test_what_now_cli_is_read_only_and_json_emits_ranked_answer` passed: 4 tests.
- `PYTHONPATH=$PWD/src python -m pytest` passed: 452 tests.
- `PYTHONPATH=$PWD/src ruff check src tests` passed.
- `PYTHONPATH=$PWD/src python -m bureau.cli --root . check` passed.
- `PYTHONPATH=$PWD/src python -m bureau.cli --root . --json doctor` reported `healthy=true`.
- `git diff --check` passed.
- Smoke: `PYTHONPATH=$PWD/src python -m bureau.cli --root . --json what-now --capability repository --capability shell --capability grabowski --limit 3` selected `BUR-2026-003-T007` with `claim_eligible=false` and soft reasons `state is planned`, `execution is interactive-agent/review-before-effect`.

## Non-claims

This review does not establish claim authority, workspace creation, dispatch authority, merge readiness, queue mutation, task approval or that every future ranking concern is already modeled.
