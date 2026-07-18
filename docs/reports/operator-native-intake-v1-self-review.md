# Operator-native Bureau intake v1 — Self-Review

## Binding

- Base: `a7e306aa414584b0dba7873b57d5702c3a99e086`
- Reviewed head: `d961d96025b03076706f0a0a1e3870257c4221c4`
- Reviewed tree: `7bd217aa8ae6f15fc91f4989fd744fd9fa9202d7`
- Reviewed binary diff SHA-256: `919edd8d076136362f3db3227608d0a7f8ab604f4f143e40251d9158adb20df9`
- Verdict: **PASS**

## Reviewed scope

1. Define ChatGPT through Grabowski as executing operator and the user as observer and steersman.
2. Make machine operability, not human execution convenience, the primary interface criterion.
3. Register `OPERATOR-MACHINE-READABILITY-V1-T017` for typed candidate recording, assessment, reviewed proposal and PR publication.
4. Reframe `BUR-2026-003-T008` as a Steuerboard-only source adapter to the canonical T017 domain surface.
5. Preserve Live Register, Registry, queue, claim, dispatch, merge and deployment authority boundaries.

## Dialectical review

### Benefit

The change removes a wrong optimization premise: CLI quality is no longer evaluated as a human-facing product. Typed Grabowski consumption, idempotency, provenance, bounded output, failure codes, readback and receipts become the primary design target. The user retains high-value observer and steering surfaces without becoming an operational dependency.

### Counter-risk

Calling ChatGPT the operator could be misread as unlimited authority. The plan and task therefore retain explicit cost, safety, privacy, irreversibility and approval gates. `Review` is defined as policy- and evidence-bound; operator self-review is allowed only where the active contract permits it.

### Alternative axis

A fully human-oriented UI would improve manual usability but preserve the wrong operating model. A pure CLI-only approach would be machine-callable, but would keep schema discovery, orchestration and error reconciliation implicit. The selected path uses one Bureau domain core with thin CLI and typed Grabowski adapters; human dashboards remain read-only observer projections.

## Findings and corrections during review

- Corrected the initial wording so observer dashboards remain valuable for steering but are never execution prerequisites.
- Corrected review semantics so `reviewed_plan` does not imply routine manual human work.
- Avoided falsifying the initiative's historical `new_task_count`; T017 is recorded separately under `additional_registered_tasks`.
- Kept semantic similarity advisory only; no automatic merge, close, suppression or deduplication authority is granted.
- Kept HTTP out of scope until a concrete remote consumer exists.
- Fixed Codex P2 review finding: the task approval class is now `registry_mutation`, whose runtime rule actually requires `reviewed_plan`; operator-level evidence is rejected and reviewed-plan evidence is accepted.
- Fixed the deeper schema/runtime drift exposed by that finding: the task schema action-class enum now exactly matches `READ_ONLY_ACTIONS | APPROVAL_RULES`, including `dry_run`, `registry_mutation`, `worktree_cleanup` and `state_root_migration`.
- Rebased after Bureau PR #668 merged as orthogonal Registry task truth; the implementation diff digest remained unchanged, while base and tree bindings were renewed and all 50 test files were rerun.

## Validation

- `ruff check src tests` — PASS.
- All 50 `tests/test_*.py` files executed successfully in five bounded chunks; every completed chunk returned 0.
- The earlier monolithic `make validate` and oversized final chunk were terminated only by the 30-second synchronous tool limit and were not counted as evidence.
- Focused Registry, queue, Live Register and lease-contract tests — PASS.
- Approval-contract regression probe — `registry_mutation` requires `reviewed_plan`; operator approval rejected; reviewed-plan approval accepted.
- Local source-bound `bureau --root . check` — `valid: True`, 63 initiatives, 537 tasks, 43 resources.
- Systemkatalog static-boundary validation — PASS, no violations.
- `git diff --cached --check` before the implementation commit — PASS.

## Non-claims

This review does not establish implementation of T017, queue truth, task readiness, claim or dispatch authority, semantic deduplication correctness, merge readiness, deployment success or permission to bypass explicit steering and safety gates.
