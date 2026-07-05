# Bureau Boundary/Ops Plan v1

Status: planned
Owner layer: Bureau coordination
Primary implementation repo: `heimgewebe/bureau`
Created: 2026-07-05

## Thesis / antithesis / synthesis

Thesis: Bureau's core is valuable because it is small: durable intent in Git, volatile execution in SQLite, revision-bound claims, envelopes and receipts.

Antithesis: The repository also contains operational organs such as closure, review stewardship, Codex bridging, agent frontier, discovery and Cabinet bridges. They are useful, but they can blur Bureau's boundary when their mandate is not explicit.

Synthesis: Keep Bureau as a small coordination core and document the operational organs as explicit consumers around it. Organs may observe, derive, propose and receipt. They do not replace GitHub, Grabowski, Steuerboard, Cabinet, Schauwerk or Chronik.

## Alternative axis

Do not optimize for moving files first. Optimize for this question:

> Can a fresh operator tell which layer owns a decision, which layer merely observed it, and which evidence is still missing?

Extraction into a separate `bureau-ops` package is a later decision, not the first step.

## Source weighting

1. Primary contracts and runtime invariants: schemas, registry validation, SQLite migration behaviour, receipts and reconciliation rules.
2. Primary operational code: dispatch, closure, review stewardship, Codex bridge, agent frontier, discovery and Cabinet bridge modules.
3. Operational deployment facts: systemd units, state-root layout and current entry points.
4. Documentation and README claims.
5. Operator impressions and LOC ratios.

## Scope

### In scope

- Document a Core/Ops/External Authority component model.
- Register the resulting work as Bureau tasks.
- Make README, architecture and ownership documentation match the real surface.
- Add follow-up tasks for state-root hygiene, legacy audit, entry-point consolidation and ops extraction assessment.

### Deferred

- Deleting `legacy.py`.
- Removing console entry points.
- Moving operational organs into a new repository.
- Changing dispatch, closure or review semantics without a focused follow-up task.

## Organ map

| Organ | Role |
|---|---|
| Bureau Core | Commitments, queue order, dependencies, claims, envelopes and revision-bound receipts. |
| Bureau Ops | Observes external systems, derives findings, proposes tasks and materialises explicit receipts. |
| GitHub | Branches, pull requests, reviews and CI facts. |
| Grabowski | Processes, hosts, leases, durable jobs and concrete workers. |
| Steuerboard | Action-specific readiness and specialised evidence. |
| Cabinet | Readable research, synthesis and decisions. |
| Schauwerk | Visual projections. |
| Chronik | Append-only events. |

## Task sequence

1. **T001 — Boundary documentation.** Update README, architecture and ownership docs so the real Core/Ops/Authority model is explicit.
2. **T002 — State-root doctor.** Add a read-only doctor check for unknown files in the Bureau state root and document a manual cleanup path.
3. **T003 — Legacy dependency audit.** Produce a dependency map for `legacy.py`, including imports, re-exports, CLI users, tests and stored-state compatibility.
4. **T004 — Console entry-point consolidation plan.** Inventory all console scripts, systemd references and compatibility risks; add a migration plan before changing binaries.
5. **T005 — Ops extraction decision.** Decide whether operational organs stay in-repo behind explicit boundaries or move to `bureau-ops`, based on the results of T001-T004.

## Risk / benefit check

Benefits:
- Less authority drift.
- Clearer onboarding for future operators.
- Smaller blast radius for later refactors.
- Better basis for deciding whether `bureau-ops` is justified.

Risks:
- A documentation-first slice may be too weak if real drift exists in code.
- Premature extraction would add package and deployment complexity.
- Legacy removal or entry-point consolidation could break hidden users or systemd units if done without inventories.

Mitigation:
- Keep the first slice explicit about what it does and does not prove.
- Keep code-moving tasks behind dependency audits.
- Treat state-root cleanup as read-only reporting before deletion.
