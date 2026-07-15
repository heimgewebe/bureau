# RepoBrief Practical Utility Reconcile v1

This plan reconciles the user-provided RepoBrief v1 roadmap, the Lenskit-internal task registration from Lenskit PR #911, the Bureau RBV1/RBAE/RBAW registries, and the rLens usefulness findings from the full max-dump test.

## Correction

Operational tasks must live in Bureau. Lenskit `docs/tasks/*` may mirror repository-local planning state, but Bureau is the cross-repository work queue for Grabowski/operator execution.

Lenskit PR #911 was useful as repo-local documentation, but it did not by itself make the tasks schedulable by Bureau.

## Live basis

Observed on 2026-07-08:

- Lenskit `origin/main`: `61470f13` after PR #911.
- Bureau `origin/main`: `b3010c2` after PR #180.
- Open Bureau PR #177 changes claim-guard code/docs only; it does not touch registry task files or queue entries used here.

## RepoBrief v1 roadmap reconciliation

Already verified in Bureau:

- `RBV1-T001` naming and vocabulary.
- `RBV1-T002` explicit snapshot create.
- `RBV1-T003` snapshot profiles.
- `RBV1-T004` health degradation states.
- `RBV1-T005` Git provenance status.
- `RBV1-T006` availability/freshness model.
- `RBV1-T007` CLI alias.
- `RBV1-T008` CLI migration documentation.
- `RBV1-T009` export safety by profile.
- `RBV1-T010` read-only access / MCP-boundary-equivalent surfaces, with the clarification below.
- `RBV1-T014` guard relation goldset.
- `RBV1-T015` graph availability.
- `RBV1-T016` Python AST symbol index.
- `RBV1-T017` retrieval v2 promotion gate/evaluation machinery.

Closed by this reconciliation:

- `RBV1-T012` contract CI path filters, implemented in Lenskit PR #905.
- `RBV1-T013` Ruff ratchet scope, implemented in Lenskit PR #910.

Still open in the original RBV1 lane:

- `RBV1-T011` explicit MCP `snapshot_create`.
- `RBV1-T018` package/repository rename decision.

## RBV1-T010 clarification

`RBV1-T010` was verified as read-only RepoBrief access and MCP-boundary-compatible resource/tool-equivalent behavior. It does not establish that a protocol-level MCP server with concrete `repobrief://snapshot/...` resources is implemented.

A follow-up practical utility task therefore registers the real MCP resource implementation gap separately.

## Lenskit PR #911 task mapping

The Lenskit-local tasks from PR #911 are mapped as follows:

| Lenskit-local task | Bureau treatment |
| --- | --- |
| `TASK-REPOBRIEF-SYMBOL-INDEX-CONSUMER-001` | `RPU-V1-T007` |
| `TASK-REPOBRIEF-MCP-READONLY-RESOURCES-001` | `RPU-V1-T020` because `RBV1-T010` only proves boundary/access-equivalent surfaces |
| `TASK-REPOBRIEF-MCP-SNAPSHOT-CREATE-001` | existing `RBV1-T011` |
| `TASK-RUNTIME-ARTIFACT-RETENTION-001` | `RPU-V1-T008` |
| `TASK-GUARD-RELATION-PERSISTENCE-DECISION-001` | `RPU-V1-T009` |
| `TASK-RETRIEVAL-V2-DEFAULT-PROMOTION-DECISION-001` | `RPU-V1-T011` |
| `TASK-GRAPH-DEGRADATION-SEMANTICS-HARDENING-001` | `RPU-V1-T010` |
| `TASK-REPOBRIEF-WORKBENCH-USEFULNESS-EVAL-001` | `RPU-V1-T012` with links to `BUR-2026-002-T005` and `RBAE-V1-T006` |
| `TASK-REPOBRIEF-PACKAGE-RENAME-DECISION-001` | existing `RBV1-T018` |
| `TASK-REPOBRIEF-READONLY-ADAPTER-NO-MIRROR-001` | `RPU-V1-T001`, `RPU-V1-T003`, and `RPU-V1-T020` depending on access channel |

## Additional useful tasks from the max-dump critique and follow-up vision

The full rLens/Lenskit max-dump critique identified practical utility gaps not covered by the original RBV1 plan:

1. Resolved evidence query that returns content, path, lines, citation id, range ref and freshness in one call.
2. Live-repo citation addressing beside canonical bundle addressing.
3. Latest-complete/freshness automation without hidden refresh on reads.
4. Token-budget context compiler.
5. Delta-Lens / PR delta context compiler.
6. Proof-of-reading coverage for review evidence.
7. Compact card density and non-claim de-duplication for token consumers.
8. Contract hygiene for range refs, top chunk spans and query JSON line numbers.
9. SemantAH chunk embedding bridge as an external semantic layer over stable RepoBrief chunks.
10. Claim evidence revalidation for Cabinet claims.
11. Verifiable agent memory using citation ids and freshness checks.
12. Federated fleet search across bundles.
13. Runtime-Lens bridge to Chronik/runtime evidence without collapsing authority boundaries.

## Optimized sequence

Near-term:

1. `RPU-V1-T001` resolved evidence query.
2. `RPU-V1-T002` live-repo citation addressing.
3. `BUR-2026-002-T002` rLens context pack bridge, now dependent on resolved evidence.
4. `BUR-2026-002-T003` rLens mode enforcement.
5. `RPU-V1-T019` contract hygiene fixes.
6. `RPU-V1-T003` latest-complete freshness registry.

Middle:

7. `RPU-V1-T004` token-budget context compiler.
8. `RPU-V1-T005` delta-lens / PR delta context compiler.
9. `RPU-V1-T006` proof-of-reading coverage.
10. `RPU-V1-T007` symbol index consumer wiring.
11. `RPU-V1-T008` runtime artifact retention.
12. `RPU-V1-T010` graph degradation semantics hardening.

Later:

13. `RPU-V1-T011` retrieval v2 default-promotion decision.
14. `RPU-V1-T012` workbench usefulness evaluation.
15. `RPU-V1-T013` SemantAH chunk embedding bridge.
16. `RPU-V1-T014` Cabinet Claim-TÜV.
17. `RPU-V1-T015` verifiable agent memory.
18. `RPU-V1-T016` federated fleet search.
19. `RPU-V1-T017` Runtime-Lens bridge.
20. `RPU-V1-T018` compact card density.
21. `RPU-V1-T020` real MCP read-only resources.
22. Existing `RBV1-T011` MCP snapshot_create.
23. Existing `RBV1-T018` package/repo rename decision.

## Non-claims

This reconciliation does not implement the registered technical tasks, prove RepoBrief improves agent quality, prove runtime correctness, prove test sufficiency, prove review completeness, authorize any merge, or make MCP resources available. It ensures the work is represented in Bureau with explicit boundaries and dependencies.

## Live reconciliation update — 2026-07-15

Lenskit PR #1014 changed only fleet timer documentation, one timer unit and a test below `merger/lenskit`. The live fleet run nevertheless classified all 40 repositories as changed because `rb-publish-fleet` fingerprints the complete `HEAD:merger/lenskit` tree. Eight repositories were immediately republished and 32 queued.

This is not a retention/reachability fix and therefore does not expand `RBV1-T026`. It is registered separately as `RPU-V1-T022`: the publication fingerprint must be bound to explicit effective generator inputs, while real source, generator, contract and relevant configuration changes must continue to invalidate fail-closed. Implementation waits for `RBV1-T026` because both tasks touch the same fleet publisher and tests.
