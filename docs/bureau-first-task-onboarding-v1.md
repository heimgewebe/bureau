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
4. the proposed TaskSpec contains exactly one claim for that repository with
   `mode=write` and `isolation=worktree`;
5. the normal operator-intake candidate, Registry snapshot, proposal digest and
   `reviewed_plan` approval are still exact and current;
6. publication holds the exact live StateStore-path lease whose metadata is bound to the
   onboarding proposal and authority.

The pure policy validator in `bureau.first_task_onboarding` establishes only conditions 1-4.
The operator-intake integration must establish 5-6 immediately before the write.

## Lease identity

For the bootstrap publication, the proposed TaskSpec id may be used as the lease `task_id`
**only as an exact binding label**. It does not mean that TaskSpec already exists and it does
not create ordinary publisher authority. The authority comes from the reviewed onboarding
proposal plus the create-only checks above.

After the first TaskSpec is created, this exception is no longer available for that repository.
Subsequent TaskSpecs use the ordinary publisher-task path.

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
- permission to reuse an unrelated TaskSpec as publisher.

## Intended commonthing bootstrap

For the current `repo.commonthing` case, the visible-cardinality follow-up should be published
first through this onboarding path. Once that TaskSpec exists, the BBOX-freshness follow-up can
use the newly authoritative commonthing TaskSpec as the normal publishing task. This keeps the
exception one-shot rather than turning onboarding into a parallel task-publication system.
