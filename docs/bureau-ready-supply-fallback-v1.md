# Bureau claimable-supply fallback v1

## Purpose

The task-supply contract prevents a large Registry from appearing operational when its current claimable frontier is empty or too small. It distinguishes documents that merely say `state: ready` from tasks that the current Bureau dispatcher can actually claim after capabilities, dependencies, active runs, leases, open pull requests, runtime health, and approval policy are evaluated.

The contract is deliberately conservative. A fallback proposal is not a task, a lease, or dispatch authority. It becomes eligible only after a reviewed, revision-bound publication writes a valid task document and queue entry to the canonical Registry and the normal claim path evaluates it again.

## Supply policy

Version 1 uses two thresholds:

- **Floor:** at least 8 currently claimable tasks.
- **Refill target:** 12 claimable tasks by default.

Refill is triggered only when current claimable supply falls below the floor. Once triggered, a cycle proposes work toward the higher target, bounded by `max_new_per_cycle` blocker-free publication candidates (4 by default). Blocked catalog entries remain visible but do not consume that limit. Reaching the floor again stops new proposals. This hysteresis avoids creating one replacement task after every individual claim.

The report exposes separate counts for:

- raw ready tasks;
- normally claimable work;
- claimable fallback work;
- ready but blocked tasks;
- shortage to the refill target.

Normal work always remains ahead of fallback work. Generated fallback tasks use the `later` lane and high deterministic ranks. The agent frontier continues to select a normal candidate before recommending fallback publication.

## Deterministic catalog

The bounded catalog contains seven categories in stable order:

1. maintenance;
2. care;
3. audit;
4. diagnosis;
5. registry reconciliation;
6. queue reconciliation;
7. error investigation.

Every catalog entry declares:

- a bounded goal;
- required capabilities;
- a Registry resource claim;
- an exact Grabowski path scope;
- acceptance assertions;
- review-before-effect policy.

The catalog does not synthesize arbitrary user-facing product work and does not invent evidence. It supplies only bounded operational work that can be revalidated against current system truth.

## Identity, reuse, and bounded growth

Each category has two identifiers:

- `open_key`: stable across time buckets for the same category, repository, initiative, resource claim, and path scope;
- `fingerprint`: the open key plus a bounded time bucket.

A nonterminal canonical task with the same open key is reused. A later cycle therefore does not create a duplicate merely because the fingerprint bucket changed. Terminal tasks may be replaced in a later bucket. Each cycle is additionally capped by `max_new_per_cycle` and the finite catalog size.

A terminal fallback keeps its canonical task document, and that document keeps the id derived from the current bucket. Recreating the identical id would abort the whole publication, so a category whose current-bucket id already exists canonically carries the blocker `fallback-task-id-already-canonical-in-current-bucket` and waits for the next bucket. The remaining categories still refill, so a fast-closing fallback cannot deadlock the supply loop.

## Supply stage

`python -m bureau.supply_runner` closes the loop that preview alone cannot: it produces the authoritative snapshot the preview demands and writes the report the agent frontier reads.

One cycle:

1. reads the current Registry Git head and the queue SHA-256;
2. observes the frontier through the canonical `Dispatcher`, using the same runtime execution truth the claim path gates on and the worker capabilities passed with `--capability`;
3. persists that observation as a bounded, revision-bound snapshot (`frontier-snapshot.json`) restricted to the fields the claim contract exposes;
4. writes the supply report to `latest-report.json` under the state root that `bureau-agent-frontier` already consumes;
5. publishes the bounded plan **only** with both `--mutation-authority` and `--publish`, and only when the plan is authorized and blocker-free;
6. reads the published tasks back through the dispatcher and records whether the normal gates now admit them;
7. emits a terminal cycle receipt.

Without `--mutation-authority` the stage stays a preview: a starved frontier yields status `blocked` with the explicit `registry-mutation-authority-unavailable` blocker and leaves the Registry byte-identical. Capabilities have no default; an empty capability set is rejected rather than treated as unrestricted. The stage is not wired into a timer — mutation authority stays an operator decision per Registry revision.

## Preview and publication

`bureau.task_supply` produces a versioned report and a digest-bound publication plan.

Preview is read-only. The command requires an already authoritative frontier snapshot through `--frontier-report`; it does not instantiate or migrate the Bureau state store. The snapshot must be bound to the exact Registry head and queue digest through `--frontier-head` and `--frontier-queue-sha256`; its own SHA-256 is included in the report. Missing or mismatched bindings block publication. Runtime health is fail-closed: without the explicit `--runtime-healthy` observation, every candidate retains `required-runtime-unhealthy`. It may show proposed task documents, but every proposal explicitly states:

- current claimability for reused canonical work;
- whether canonical publication is required;
- exact safety blockers;
- what the plan does not establish.

Publication requires all of the following:

1. explicit mutation authority;
2. the reviewed plan SHA-256;
3. the exact Registry Git head observed during planning;
4. the exact queue file SHA-256 observed during planning;
5. an authorized, blocker-free plan;
6. successful post-write `Registry.load()` validation.

A changed head, changed queue, retained blocker, existing target file, malformed task binding, or failed Registry validation aborts the operation. Queue bytes and newly created task files are restored to their pre-publication state on failure.

Publication itself still does **not** establish claimability. The canonical dispatcher must re-read the task and apply its unchanged claim gates. The regression suite proves that an unpublished proposal is absent from the dispatcher, while a published task enters the ordinary pickup frontier and remains blocked by missing capabilities or review approval until those normal gates are satisfied.

Fallback TaskSpecs do not depend on an out-of-band caller to invent an acceptance contract. When no explicit acceptance mapping is supplied, the server-owned fallback catalog materializes each catalog assertion as a typed `object` criterion using a category- and criterion-bound `manual_observation` verifier scope. This is intentionally limited to semantic fallback acceptance because no implementation PR or artifact exists when the TaskSpec is created; repository, CI, merge and runtime gates remain separate lifecycle authorities. An explicit caller-provided mapping still overrides the catalog default and is validated unchanged, so an explicit empty or malformed mapping remains fail-closed.

## Safety gates

The supply report preserves exact blockers rather than flattening them into a generic unavailable state. Relevant blockers include:

- missing worker capabilities;
- unsatisfied dependencies;
- active task or initiative limits;
- conflicting leases or reservations;
- overlapping active work or open pull requests;
- dirty or foreign workspaces supplied by the operational preflight;
- unhealthy or drift-blocked required runtimes;
- missing review or mutation authority.

Explicit approval may remove only the existing `interactive-agent/review-before-effect` reason. It cannot erase any other blocker.

## Agent-frontier integration

`bureau-agent-frontier` accepts `--task-supply-report` (or `BUREAU_TASK_SUPPLY_REPORT`). It verifies the report SHA-256 before consuming any count or recommendation, embeds a compact supply summary under `scanner_summary.task_supply`, and adds a high-severity bottleneck when appropriate:

- `claimable_task_supply_below_floor`;
- `claimable_task_supply_blocked`;
- `claimable_task_supply_report_invalid`.

The frontier remains read-only. It does not publish tasks, reserve resources, claim work, dispatch agents, merge branches, or deploy a runtime. Its recommendation order is:

1. preserve existing closure-binding governance;
2. prefer selected normal frontier work;
3. resolve exact supply blockers;
4. review a bounded canonical publication plan;
5. otherwise keep observing.

## Operational sequence

Steps 1, 2, 5, 6, and 7 are the supply stage; steps 3, 4, and 8 stay with the operator.

1. Read the current Registry, state database, runtime identity, open-PR guard, capabilities, and frontier through the canonical dispatcher.
2. Persist that authoritative frontier as a revision-bound snapshot and generate a read-only supply report from it.
3. Review counts, blockers, catalog scopes, fingerprints, head, queue digest, and plan digest.
4. Grant mutation authority only for that exact plan and Registry revision.
5. Publish atomically.
6. Reload and validate the Registry.
7. Run the normal claim-intent path again.
8. Treat any post-publication blocker as authoritative; do not bypass it.

```
# 1-3: preview only, no mutation authority
python -m bureau.supply_runner --registry-root . --capability repository --capability python \
  --capability testing --capability bureau --capability grabowski

# 4-7: after reviewing the plan for this exact Registry revision
python -m bureau.supply_runner --registry-root . --capability repository --capability python \
  --capability testing --capability bureau --capability grabowski \
  --mutation-authority --publish
```

## Nonclaims

This contract does not establish:

- hidden or ephemeral work authority;
- permission to bypass leases, dirty-state policy, open-PR guards, capabilities, or runtime health;
- that a proposed task is useful after its evidence becomes stale;
- automatic merge, deployment, or production effect authority;
- that raw `ready` counts measure executable supply.
