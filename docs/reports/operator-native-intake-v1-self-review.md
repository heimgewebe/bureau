# Operator-native Bureau intake v1 — Self-Review

## Binding

- Base: `ae5a62d0949c26700d2934785224eb359973107d`
- Reviewed head: `53af6e44f86be7a562af61293720cf159af93306`
- Reviewed tree: `56eed59e99766759f68e77449de81cb170aede91`
- Reviewed binary diff SHA-256: `43a3e86c98f27e963cc7ae599aa4165cffcfde599423cd4f396231f1cbc01368`
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
- Fixed the first Codex P2 by removing the weak external-task-creation gate and proving the stronger Registry publication rule. A second exact-head review then exposed that the implementation task itself writes code. Final contract: T017 uses `repository_mutation` with operator approval, while the publication operation independently uses `registry_mutation` with `reviewed_plan`; neither gate substitutes for the other.
- Fixed the deeper schema/runtime drift exposed by that finding: the task schema action-class enum now exactly matches `READ_ONLY_ACTIONS | APPROVAL_RULES`, including `dry_run`, `registry_mutation`, `worktree_cleanup` and `state_root_migration`.
- Rebased after Bureau PR #668 and the subsequent RepoGround and failure-domain-capacity changes through main `ae5a62d` merged. The new coordination-scope additions touch the same task schema at an orthogonal location; the rebase was conflict-free, schema/runtime Approval equality still passes, and all 51 test files were rerun.

## Validation

- `ruff check src tests` — PASS.
- All 51 `tests/test_*.py` files executed successfully in five bounded chunks; every completed chunk returned 0.
- The earlier monolithic `make validate` and oversized final chunk were terminated only by the 30-second synchronous tool limit and were not counted as evidence.
- Focused Registry, queue, Live Register and lease-contract tests — PASS.
- Approval-contract regression probe — T017 task contract resolves to `repository_mutation/operator`; operator approval is accepted for implementation. Separate publication evaluation resolves to `registry_mutation/reviewed_plan`; operator approval is rejected and reviewed-plan approval is accepted.
- Local source-bound `bureau --root . check` — `valid: True`, 63 initiatives, 537 tasks, 43 resources.
- Systemkatalog static-boundary validation — PASS, no violations.
- `git diff --cached --check` before the implementation commit — PASS.

## Non-claims

This review does not establish implementation of T017, queue truth, task readiness, claim or dispatch authority, semantic deduplication correctness, merge readiness, deployment success or permission to bypass explicit steering and safety gates.
