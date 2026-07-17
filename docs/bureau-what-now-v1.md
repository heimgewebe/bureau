# Bureau What-now v1

Status: closes `BUR-2026-003-T007`.

## Decision

`bureau what-now` is a read-only operator entrypoint for choosing the next useful Bureau ball from registry truth. It does not claim a run, create a workspace, dispatch an agent, mutate the queue or depend on chat memory.

The command answers three questions:

1. Which task is the next operator-eligible ball?
2. Why is it ranked there?
3. Which tasks are blocked, and by which durable reasons?

## Eligibility model

`what-now` deliberately separates operator attention from autonomous claiming:

- `eligible=true` means the task is suitable for operator attention according to registry state, queue position, dependency state, capability context, resource claims and runtime blockers.
- `claim_eligible=true` means the stricter `claim-next` path could claim it immediately.
- Planned `interactive-agent/review-before-effect` tasks are operator-eligible but not claim-eligible. Their soft reasons are preserved in `soft_reasons`.
- Hard blockers remain in `blocker_reasons` and are summarized by `blocker_summary.reason_counts`.

## Ranking contract

The ranking is deterministic and derived from registry/runtime facts only:

1. Queue lane order.
2. Queue index.
3. Task priority rank.
4. Task id.

Eligibility inputs include task state, initiative state and commitment, execution policy, capabilities, dependency states, active runs, reservations, resource claim conflicts, open PR guard output and RLens policy.

## Output shape

The JSON response contains:

- `selected`: first operator-eligible task, or `null`.
- `ranked_eligible`: bounded list of operator-eligible tasks.
- `blocked`: bounded list of tasks with hard blockers.
- `blocker_summary`: aggregate hard blocker counts.
- `runtime_truth`: existing frontier runtime truth for strict claim availability.
- `ranking_contract`: explicit source/order/does-not-use declaration.
- `does_not_establish`: non-authority boundaries.

## Performance and compact projection

Open pull-request safety remains fresh and fail-closed, but distinct repositories are queried concurrently with a bounded worker pool. `BUREAU_OPEN_PR_CLAIM_GUARD_WORKERS` may tune the pool from 1 to 32; invalid values fall back to 8. No cache or stale snapshot is used for claim safety.

Use `bureau what-now --compact` when an operator or machine consumer needs the decision rather than the complete evidence envelope. The compact projection keeps selected tasks, bounded blockers, top blocker counts, lifecycle inconsistencies and live-register counts while omitting full task claims, approval envelopes, lifecycle task maps and candidate history. The default output remains backward-compatible and complete.

## Boundary

`what-now` is a read-only answer. It is not a claim, checkout, dispatch, merge, queue repair or approval gate. It can recommend that a planned review-before-effect task should be handled next, but the follow-up mutation must still go through the relevant approval and review path.
