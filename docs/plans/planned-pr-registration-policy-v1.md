# Planned PR registration policy v1

Status: active

## Rule

All planned PR slices that are more than transient ideas must be visible in Bureau. Bureau is the planned-work registry: it makes slices deduplicable, orderable and claimable without making every idea immediately executable.

## State model

- candidate: visible potential slice, not claimable. In the current task schema this is represented as `state: inbox` plus `metadata.planning_stage: candidate`.
- planned: accepted planned slice, not yet claimable by default. In the task schema this is `state: planned`.
- ready: claimable / ausschreibbar. In the task schema this is `state: ready`.

Only `ready` slices should be treated as claimable work. `candidate` and `planned` remain coordination inventory until promoted.

## Authority split

- Source repositories own fachliche plans and implementation details.
- Bureau owns the planned implementation list, dependency ordering, claimability state and completion evidence requirements.
- Grabowski executes the grips.
- Cabinet remains Sinn, Karte and decision context; it is not the execution queue.
- Chronik and Plexer remain evidence / transport layers; they do not become task authority.

## Completion rule

A slice is not complete merely because a branch exists. Completion needs a receipt appropriate to its impact class. For PR slices, expected evidence includes PR URL, diff, tests or validators, and any sample receipts produced by the implementation.
