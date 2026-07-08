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
- Integration and deployment are exclusive tasks.
- Reconciliation runs before each checkout.

Compatibility: read/read is allowed; write conflicts with read or write; exclusive conflicts with
all access; capacity claims are allowed until the declared capacity is exhausted. A parent resource
overlaps all descendants.
