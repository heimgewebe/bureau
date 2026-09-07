# Bureau first-TaskSpec onboarding v1

## Problem

Normal operator-intake publication is intentionally self-hosted: an existing authoritative
TaskSpec identifies the publishing task and owns the exact StateStore-path lease used for the
write. That rule becomes circular when an already cataloged repository has no authoritative
TaskSpec yet. Its first task cannot already exist in order to authorize its own creation.

The onboarding contract closes only that bootstrap gap. It is not a general publication
bypass.

## Authority boundary

First-task onboarding may be derived only when all of the following are freshly proven:

1. the target Registry resource exists and has type `git-repository`;
2. the proposed TaskSpec does not already exist in StateStore;
3. no authoritative TaskSpec currently claims or overlaps the target repository resource;
4. the proposed TaskSpec contains only one claim, for that repository with
   `mode=write` and `isolation=worktree`; additional resources require ordinary publication;
5. the normal operator-intake candidate, Registry snapshot, proposal digest and
   `reviewed_plan` approval are still exact and current;
6. publication holds the exact live StateStore-path lease whose metadata is bound to the
   onboarding proposal and authority.

The pure policy validator in `bureau.first_task_onboarding` establishes only conditions 1-4
from the observations supplied to that validation call. Those observations are snapshot-sensitive:
the operator-intake integration re-derives the current Registry resource and the complete
current StateStore TaskSpec/claim view at the publication boundary, then establishes 5-6
immediately before the write. A returned onboarding authority is not a durable freshness token
or reusable publication authorization and must not be carried across an intervening state
change.

Registry ancestry determines overlap in both directions, including parent and child resource
claims. All authoritative TaskSpecs count, including terminal tasks and read claims. Git task
files are compatibility projections, not a substitute for the StateStore claim view. Unreadable
TaskSpec state or claims whose resources cannot be resolved fail closed.

## Operator-intake integration

Use the existing `task_propose` / `operator-task-propose` interface with
`publishing_task_id` equal to the proposed task's `id`. Onboarding is selected only if that
publisher is absent from StateStore. The current candidate's exact `repo` identifies the target
Registry repository. An unrelated absent publisher still fails with `publishing-task-unknown`.
An existing publisher continues through the ordinary publication path.

The immutable proposal includes `first_task_onboarding`, a null `publishing_task_sha256`, and
a `register` TaskSpec binding with a null StateStore preimage. The proposal digest covers the
onboarding binding. Review uses the existing exact-digest `review_task_proposal` interface;
review records approval, not freshness. Preview revalidates the live facts before reporting
`ready` and exposes `lease_task_id` and `required_lease_metadata` for onboarding.

Publication retains the reviewed operator-intake TaskSpec CAS, mutation idempotency key,
projection readback, lease release and receipt path. Before a new onboarding write, it holds
SQLite `BEGIN IMMEDIATE` transactions on the lease database and StateStore. It then reloads
the reviewed proposal, current candidate and canonical Registry, and checks the complete
authoritative TaskSpec claim view. The candidate event identity, full event digest and injected
task metadata must agree. The Registry commit and tree must equal the proposal's identity.
The proposal must still authorize only StateStore registration, with no queue mutation.

The exact live lease is read again under those locks. Registry identity and proposal bytes are
checked again immediately before `task_specs.put` uses the held StateStore transaction.
The lease database remains locked through the StateStore commit. This orders concurrent
StateStore and lease writers; Registry and proposal files remain external filesystem inputs
subject to the existing clean canonical Registry and bounded regular-file checks, not a
cross-filesystem transaction. No preview or policy return value can authorize a later write.

## Lease identity

For the bootstrap publication, the proposed TaskSpec id may be used as the lease `task_id`
**only as an exact binding label**. It does not mean that TaskSpec already exists and it does
not create ordinary publisher authority. The authority comes from the reviewed onboarding
proposal plus the create-only checks above.

After the first TaskSpec is created, this exception is no longer available for that repository.
Subsequent TaskSpecs use the ordinary publisher-task path.

The required resource key is exactly `path:<resolved live StateStore root>`. The live row must
belong to the requested owner, have at least the existing 60-second remaining lease margin,
and carry valid digest-bound metadata containing:

```text
task_id: <proposed TaskSpec id>
operation: state-task-publication
proposal_sha256: <exact reviewed proposal digest>
authority_kind: bureau_first_task_onboarding_authority
first_task_onboarding_sha256: <canonical SHA-256 of proposal.first_task_onboarding>
```

Callers must use the onboarding preview's `required_lease_metadata` when acquiring the lease.
Ordinary publication retains its existing three metadata fields. No lease is acquired by the
policy validator, review or preview, and onboarding does not grant repository execution leases.

## Replay and recovery

Completed receipt replay keeps the #2267 contract: validate the exact reviewed plan, receipt,
TaskSpec history and mutation binding without consulting a current Registry, candidate or live
lease. Later Registry drift or expired leases do not invalidate a proven completed publication.

If registration committed before projection or receipt completion failed, recovery requires
the exact revision-1 digest and the original `operator-intake:<proposal_sha256>` mutation row
with a null expected revision and resulting revision 1. An identical task created by another
mutation is rejected. Proven recovery resumes the existing idempotent publication path; it
does not re-establish first-task authority or require the repository to be unclaimed again.
It still uses the reviewed plan, current candidate, schema/semantic validation and exact live
lease required by pre-receipt recovery. The completed-receipt replay validator is unchanged.

## Fail-closed properties

Onboarding refuses when the resource is not a Git repository, when the target TaskSpec already
exists, when the repository claim is missing/duplicated/weaker/different, or when any existing
authoritative TaskSpec overlaps the repository.

The contract does **not** establish:

- Registry mutation authority;
- queue mutation authority;
- TaskSpec revision authority;
- authority to publish a second task for the same repository;
- approval without a `reviewed_plan`;
- StateStore mutation without an exact live lease;
- Registry or StateStore freshness beyond the validation call;
- reusable publication authority after an intervening state change;
- permission to reuse an unrelated TaskSpec as publisher.

## Intended commonthing bootstrap

For the current `repo.commonthing` case, the visible-cardinality follow-up should be published
first through this onboarding path. Once that TaskSpec exists, the BBOX-freshness follow-up can
use the newly authoritative commonthing TaskSpec as the normal publishing task. This keeps the
exception one-shot rather than turning onboarding into a parallel task-publication system.
