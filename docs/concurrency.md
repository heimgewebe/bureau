# Concurrency contract

- At most one active run exists per task.
- An interactive worker has at most one active assignment.
- Task selection and reservations commit in one SQLite transaction.
- Queue reading is not a lock; repo-write workers must enter through `claim-next` or `checkout-next`.
- `claim-next`/`checkout-next` reserve the run before workspace, branch or PR creation.
- Incompatible reservations never overlap.
- A run binds immutable task and plan hashes.
- Scope expansion requires an explicit claim amendment.
- Parallel Git writers use distinct baseline-bound worktrees and branches.
- Open PRs are external reservations: same-task PRs block as duplicates, other open PRs block repo writes conservatively.
- Open PR task binding prefers structured markers documented in `docs/contracts/open-pr-task-metadata-v1.md`; title/body/branch matches are fallback only.
- `registry/queue.json` is the dispatch canon. Task `priority` fields are advisory/display metadata only; a task absent from the queue is not claimable by dispatcher selection.
- Open PR observation uses `BUREAU_OPEN_PR_CLAIM_GUARD_LIMIT` (default 500). If the observed page reaches that cap, coverage is explicitly bounded and the affected repository fails closed instead of silently treating the sample as complete.
- Integration and deployment are exclusive tasks.
- Reconciliation runs before each checkout.

Compatibility: read/read is allowed; write conflicts with read or write; exclusive conflicts with
all access; capacity claims are allowed until the declared capacity is exhausted. A parent resource
overlaps all descendants.

## Repository-scoped balls

Ball-vor-Board is repository-scoped for repository work. A repository ball is the current active run
or next eligible queued task for one `repo.*` resource. Bureau exposes this as a read-only
projection through `repo-balls` and as a resource filter on `frontier`, `explain-next`, `claim-next`
and `checkout-next`.

The projection is not a second queue and does not promote tasks between lanes. `registry/queue.json`
remains the dispatch canon; `task.priority` remains advisory metadata. A repository filter only
constrains which task claims are considered. Normal reservation overlap, capability, dependency,
lifecycle, open-PR and rLens gates still apply.

Because the state database keeps one active assignment per worker ID, parallel repository balls must
use distinct stable worker IDs. The recommended convention is `worker-<repo-id-with-dashes>`, for
example `worker-repo-bureau` and `worker-repo-lenskit`.
