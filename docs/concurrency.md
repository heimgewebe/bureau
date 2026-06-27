# Concurrency contract

- At most one active run exists per task.
- An interactive worker has at most one active assignment.
- Task selection and reservations commit in one SQLite transaction.
- Incompatible reservations never overlap.
- A run binds an immutable task hash.
- Scope expansion requires an explicit claim amendment.
- Parallel Git writers use distinct worktrees and branches.
- Integration and deployment are exclusive tasks.

Compatibility: read/read is allowed; write conflicts with read or write; exclusive conflicts with
all access; capacity claims are allowed until the declared capacity is exhausted. A parent resource
overlaps all descendants.
