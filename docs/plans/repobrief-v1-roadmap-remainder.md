# RepoBrief v1 Roadmap Remainder

Registers remaining RepoBrief v1 tasks from the user-provided plan.

Already treated as done or out of scope for new tasks:

- Phase 0 / PR 0.1: worktree preparation.
- Phase 4 / PR 6: read-only access layer, completed via Lenskit PR #887.
- Phase 6 / PR 11: Agent Consumption Preflight, completed earlier.
- Phase 7 / PR 12: MCP Boundary Docs, completed earlier.

Registered remainder:

1. Naming and vocabulary.
2. Explicit snapshot create command.
3. Snapshot profiles.
4. Health degradation states.
5. Git provenance.
6. Availability/freshness model.
7. RepoBrief CLI alias.
8. CLI migration documentation.
9. Export safety by profile.
10. MCP read-only resources.
11. MCP explicit snapshot_create.
12. Contract CI path filters.
13. Ruff ratchet.
14. Relation guard goldset.
15. Graph availability.
16. Python AST symbol index.
17. Retrieval v2 promotion evaluation.
18. Package/repo rename decision.

Non-claims: this registration does not implement the tasks, prove runtime correctness, prove test sufficiency, or make future merge decisions.


## Reconciliation update — 2026-07-08

Bureau now treats `RBV1-T012` and `RBV1-T013` as verified because the corresponding Lenskit work landed in PR #905 and PR #910.

`RBV1-T010` remains verified only for read-only access / MCP-boundary-equivalent surfaces. It does not prove a protocol-level MCP server with concrete `repobrief://snapshot/...` resources. That gap is registered as `RPU-V1-T020` under `REPOBRIEF-PRACTICAL-UTILITY-V1`.

Practical utility tasks discovered after the max-dump critique and Lenskit PR #911 are not stuffed back into the old RBV1 plan. They are registered under `REPOBRIEF-PRACTICAL-UTILITY-V1` so the original v1 roadmap stays readable while no useful follow-up is lost.
