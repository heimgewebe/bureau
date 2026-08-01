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
## Queue freshness

`registry/queue.json` remains the only dispatch queue. `queue-reconcile` is a read-only freshness
report over queue entries, advisory task priority and repository focus. It may recommend
`promote_to_now`, `promote_to_next`, `review_lane` or `remove_from_queue`, but it does not mutate
state. This keeps stale priority metadata visible without allowing unreviewed dispatch changes.

## Worktree hygiene

Local worktree inventory is operational context, not dispatch truth. The default
`worktree-hygiene` report remains read-only and cannot authorize cleanup. The reviewed cleanup path
accepts only explicit absolute candidate paths from one hash-bound plan, revalidates each candidate
immediately before removal and never deletes branches. Dirty, missing, locked, process-used or
unmerged worktrees fail closed.

The reviewed plan is still not a substitute for live coordination. Apply requires the short Bureau
worktree-admin effect gate and a separate current check that no foreign exact path lease covers a
candidate. This preserves Bureau's always-open object/file lease model while serializing only the
actual linked-worktree administration effect.

## Open-PR path nonconflict

An open pull request remains a repository-wide write blocker unless both sides of a narrower
comparison are complete and immutable. Bureau observes the pull-request repository, number, base
and head object IDs, every paginated changed-file record and both the old and new path of a rename.
It binds the canonical path set and full file inventory to deterministic SHA-256 digests. API
errors, invalid object IDs, malformed pagination, the configured file cap, unsafe paths or any
inconsistent digest keep the repository blocked.

A task can request a narrower comparison only through its explicit
`execution.grabowski_resources`. Every contributing `path:` resource must be absolute, canonical
and beneath the exact `execution.working_repository`; Bureau projects it to a repository-relative
POSIX path. A broad repository resource, a missing path set, the repository root, traversal, an
outside path or an ambiguous repository binding is classified as `scope-required` and does not
weaken the existing blocker. Titles, descriptions, claim names and diff heuristics never create
write authority.

Path comparison is segment-based. Equal paths and parent/child relationships conflict; similar
strings in different segments, such as `foo` and `foobar`, are disjoint. The shared projection uses
four stable classifications: `repository-blocked`, `scope-required`, `scope-conflict` and
`scope-proven-disjoint`. Missing, multiple, terminal or excepted PR task bindings remain visible as
separate governance findings. They do not by themselves turn a complete disjoint path proof into a
repository conflict, but they still establish neither merge readiness nor task correctness.

A successful coordinated claim stores the complete nonconflict assessment inside the existing
operator-approval object of the exact claim intent. It is therefore covered by the intent digest.
`claim-commit` observes all open PRs again and requires byte-equivalent assessment data. Any change
to the PR set, base or head object ID, changed paths, completeness state or task path scope blocks
before Bureau writes a run. For an already exact, proven-disjoint task scope, the coordinated intent
removes the broad `repo:` Grabowski resource and retains only the digest-bound `path:` resources.
`claim-commit` reads those live leases from Grabowski and verifies the exact resource set, owner, task,
run, intent digest and remaining lifetime before writing a Bureau run. A task that still declares a
broad repository resource remains `scope-required`; extra path declarations never narrow that broad
authority implicitly. A projection or a disjointness proof does not authorize PR mutation, merge,
deployment, work outside the declared paths or automatic repair of task bindings.

## Close-path compare-and-swap

`complete` is a compare-and-swap against the authoritative run row, not an event append. Every
expectation the close depends on — receipt absence, run state and the claim baseline of task and
plan digest — is read inside the same immediate transaction that writes the receipt, the
`verified` task status, the run transition and the reservation release. The close path therefore
holds no unsynchronised pre-reads: a run that was failed, cancelled or drifted between the caller's
decision and the write loses instead of overwriting the newer truth.

`BEGIN IMMEDIATE` orders SQLite writers and cannot order writes to the Git-backed registry, so the
baseline half of that expectation is bound to the documents rather than to the `Registry` snapshot
the caller loaded earlier. On a first close, and still inside the transaction, the last pre-effect
check resolves exactly one readable, schema-valid task document and initiative document and derives
their task and plan digests. A close proceeds only when those fresh digests equal the run's claim
baseline. The in-memory snapshot is not another gate: once the fresh documents match the frozen run
baseline, an older snapshot adds no safety and could only reject a valid close.

After all SQLite writes and the authoritative run-row readback, the same two documents are resolved
and read again as the final guard before commit. A stale revision already present before close is
therefore detected by the first read; a semantic revision change between the two in-transaction reads
is detected by the second and rolls the complete SQLite effect back. Byte-only rewrites with the same
semantic revision remain valid. `state` and `metadata.verification` stay outside the frozen task
revision, so a closure stamp does not invalidate its own binding.

This is not cross-domain atomicity. There is no cooperative registry writer lock, and a
non-cooperating writer that lands after the second document read but before the SQLite commit is not
detected by this path. The two in-transaction reads narrow the remaining unordered interval to
exactly that residual window; they do not eliminate it or exclude registry writers.

A lost close raises a typed `RunStateConflict` with a stable machine-readable `code`
(`unknown-run`, `run-not-active`, `stale-baseline`, `registry-revision-unavailable`,
`close-revision-drift`, `close-readback-mismatch`), the observed and expected state, and
`effect_applied: false`. Missing, unreadable, schema-invalid or ambiguous authoritative documents
use `registry-revision-unavailable` instead of falling back to the snapshot. The transaction rolls
back, so a rejection changes no SQLite truth and establishes neither safe retry without readback nor
task verification. The CLI emits the same object and exits 2.

The run identifier is the idempotency key. A repeated close of an already-closed run returns the
stored receipt with `idempotent: true`. Its single `BEGIN IMMEDIATE` transaction performs only the
database readback; after that transaction has ended, Bureau rewrites the receipt file and derives the
registry-backed `current` flag. A crash between the committed effect and the materialised receipt
therefore replays to the identical digest rather than producing a second effect without holding the
SQLite writer reservation across filesystem work. A revision that disagrees with the stored receipt,
or that cannot be read at all, reports `current: false`. Concurrent closes of one run produce exactly
one receipt, one `run-completed` event and explicit idempotent losers.
