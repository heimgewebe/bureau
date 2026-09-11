# Commonthing resource work view

## Purpose

`bureau.work_view` provides a read-only navigation projection around one canonical Bureau
Registry resource. The first dogfood target is `repo.commonthing`.

The view is deliberately **not** a room registry, project authority, queue, status model or
initiative. It does not create membership facts. It explains why already-authoritative Bureau
objects are displayed together.

## Why this exists

Commonthing work can be distributed across several initiatives and may have adjacent work in
other repositories. A repository-centred view makes that work visible without reintroducing the
retired Cabinet-room pattern or creating a second task/status truth.

The canonical Commonthing anchor already exists:

- resource id: `repo.commonthing`
- repository path: `/home/alex/repos/commonthing`
- GitHub repository: `heimgewebe/commonthing`
- Grabowski key: `repo:/home/alex/repos/commonthing`

## Membership classes

### `direct`

A task is direct only when the current authoritative TaskSpec has at least one deterministic
technical binding to the target resource:

1. an exact claim on the resource id;
2. an exact `execution.working_repository` path match; or
3. an exact canonical Grabowski repository key, or a typed key anchored below it such as a
   branch key.

Task titles, task ids and prose are never membership evidence.

### `related`

A non-direct task is related only when exactly one explicit task relation connects it to a
direct task. V1 understands the relations already validated by Bureau:

- `depends_on`;
- `metadata.parent_task`.

The relation is treated as navigation in either direction. V1 never expands a second hop.

### `legacy`

Legacy is opt-in at projection time. A task is legacy only when both are true:

1. it is already one-hop related to a direct task; and
2. it has an exact claim on a resource explicitly supplied through `--legacy-resource`.

For the Commonthing naming cutover the intended dogfood invocation uses
`--legacy-resource repo.weltgewebe`. A `WELTGEWEBE-*` name alone never establishes legacy
membership.

### `out`

Everything else remains outside the view. V1 reports only the count, so unrelated Bureau work
is not copied into another surface.

## Migration gaps are diagnostics, not membership

A declared legacy resource may still own nonterminal tasks that have no explicit relation to a
direct target-resource task. The view exposes these under `diagnostics.migration_gaps` instead of
silently classifying them as Commonthing work.

A migration-gap entry requires all of the following:

1. its current authoritative TaskSpec is nonterminal;
2. it is not already direct, related or legacy in the view; and
3. it has an exact claim on a resource supplied through `--legacy-resource`.

This deliberately says only: "the declared predecessor resource still has active task bindings
that are not structurally connected to the target view." It does **not** establish that the task
belongs to Commonthing, that the legacy resource is globally superseded, or that a bulk TaskSpec
rewrite is safe. Migration must be proven and performed separately.

## Candidates

Current Bureau candidate records remain candidates. They are never promoted to tasks by this
view. A candidate can be shown when its exact `repo` is the target resource or when its explicit
`task_id` points to a task already classified by the view. The returned candidate object includes
an authority boundary that states what it does not establish.

## Authority and state

The view reuses `status_projection()` for task state, queue lane, active-run and blocker
information. That matters because Bureau may have StateStore-authoritative TaskSpecs that differ
from the Git compatibility projection. `work_view` does not implement a competing effective-state
algorithm.

Initiatives are context only. The view groups visible task ids by their existing initiative but
does not make the initiative a member of a room and does not create a room-level progress score.

## Read-only contract

The projection:

- does not write Registry files;
- does not create a StateStore database when none exists;
- does not mutate Queue or TaskSpec state;
- does not create claims, leases, candidates, runs or receipts;
- does not infer task completion from runs, pull requests or candidate state;
- does not turn migration diagnostics into target-resource membership.

Every visible task or candidate carries a machine-readable `basis` explaining its inclusion.
Every migration gap carries a diagnostic-only basis and explicit non-claims.

## Dogfood command

From the Bureau repository:

```bash
python -m bureau.work_view \
  --root . \
  --resource repo.commonthing \
  --legacy-resource repo.weltgewebe
```

The result is JSON and is suitable for a future UI labelled "Commonthing" or "Raum Commonthing".
The UI label must not be interpreted as a new Bureau room authority.

## V1 stop conditions

Do not add a `room` registry, task `project_refs`, room priority, room state, transitive relation
walk, title-based classifier or automatic legacy migration in V1. If real Commonthing dogfood
shows missing cross-repository work, record the exact false negative first and only then extend
the projection contract.
