# RepoBrief Agent Workbench Boundary v1

Registered: 2026-07-06

## Status

Planning registration after Lenskit PR #889 (`feat(repobrief): Source Citation Projection v1`) was reported merged by the operator.

This document records the agreed direction for the Write-Axis question and decomposes the broad Lenskit-agent-optimization proposal into bounded Bureau work.

## Core decision

RepoBrief / rLens must not become the mutation engine.

RepoBrief remains the deterministic evidence, snapshot and citation layer:

- create explicit snapshots;
- read Brief Bundles;
- resolve Required Reading;
- report Health, Freshness and Availability;
- resolve Ranges and Citations;
- query existing indexes;
- expose read-only MCP resources and limited explicit snapshot creation later.

RepoBrief must not:

- secretly refresh;
- mutate Git;
- create PRs;
- write patches;
- execute shell/test loops as approval;
- generate review verdicts;
- claim runtime correctness, test sufficiency, security or merge readiness;
- pull embeddings or LLM reranking into the core truth layer.

## Target architecture

- RepoBrief = landkarte / evidence core / deterministic context.
- Agent Workbench = external workshop for patch application and evaluation.
- CI = independent verifier.
- GitHub PR = review and decision surface.
- Bureau = task registration, sequencing and evidence receipts.
- Codex = review organ, not source of authority.
- Human operator = decision authority for merge, risky boundaries and strategy.

## Write-Axis synthesis

Thesis: Coding agents need a Write-Axis: patch application, test/lint feedback, structured failure output.

Antithesis: putting Write-Axis inside RepoBrief blurs the authority boundary and makes a snapshot/evidence layer responsible for mutation, rollback and runtime interpretation.

Synthesis: implement Write-Axis externally as Agent Workbench / Patch Evaluation Sidecar. RepoBrief may later read and cite Workbench artifacts, but must not generate them or interpret them as approval.

## Artifact flow

1. RepoBrief Snapshot / Brief Bundle exists.
2. Agent consumes Required Reading, Ranges, Citations and Source Citation Projection.
3. Agent proposes a patch.
4. External Workbench creates an isolated Git worktree.
5. Workbench applies the patch.
6. Workbench runs explicitly configured checks.
7. Workbench emits `patch-evaluation.v1`.
8. RepoBrief may later read/link that artifact as evidence.
9. CI / PR review / human decide.

## Patch evaluation contract sketch

Future artifact: `patch-evaluation.v1.schema.json`

Candidate fields:

- `kind: repobrief.patch_evaluation`
- `version: v1`
- `input_patch_sha256`
- `base_commit`
- `worktree_provenance`
- `commands_run`
- `exit_codes`
- `stdout_excerpt`
- `stderr_excerpt`
- `changed_files`
- `test_results`
- `lint_results`
- `status: pass|warn|fail|error|not_run`
- `does_not_establish`

Required non-claims:

- does not establish correctness;
- does not establish test sufficiency;
- does not establish security;
- does not establish runtime behavior outside evaluated commands;
- does not authorize merge;
- does not establish regression absence;
- does not establish repo understanding.

## Registered work slices

1. Define Agent Workbench boundary documentation.
2. Sketch patch-evaluation artifact contract.
3. Define read-only RepoBrief consumption of patch-evaluation artifacts.
4. Prototype external Workbench harness later.
5. Triage broad Lenskit-agent-optimization axes against existing RepoBrief roadmap tasks to avoid duplicate work.

## Existing RepoBrief roadmap crosslinks

The broad Lenskit optimization proposal is not one refactor. It maps to existing or separate Bureau slices:

- Agent Contracts / schema hard-fail -> existing RepoBrief contract/CI hardening tasks, especially contract validation path filters.
- Reading Packs / Consumption -> existing Agent Consumption Preflight and output hygiene follow-ups.
- CLI JSON / Output Noise -> small future CLI hygiene slice.
- Graph / System Map Drift -> Graph Availability and Relation Goldset work.
- Citation / Evidence -> strengthened by PR #889; do not continue unless regression evidence appears.
- Anti-Hallucination AST -> read-only reference/range hints only, not patch tools.
- Retrieval / Atlas / Streaming -> Retrieval v2 promotion evaluation before any default promotion.
- Doc Freshness -> later, after Health/Provenance/Availability are stable.

## Recommended order

0. Verify PR #889 merge and update local main.
1. Boundary documentation.
2. Contract CI path filters / schema hard-fail audit.
3. Agent consumption / output JSON hygiene.
4. Graph availability.
5. Python AST symbol index.
6. Retrieval v2 promotion evaluation.
7. Patch evaluation contract.
8. External Workbench prototype.

## Non-claims

This plan does not implement the work, prove runtime correctness, prove test sufficiency, grant merge readiness, or prove that PR #889 has no regressions.
