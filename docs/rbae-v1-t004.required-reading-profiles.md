# RBAE-V1-T004 Code Required Reading Profiles

Status: closes `RBAE-V1-T004`.

## Purpose

This contract defines code-change Required Reading profiles. A profile maps one agent task kind to existing RepoBrief surfaces and to structured `pass`, `warn`, `fail` or `not_applicable` outcomes.

The contract is read-only. A resolver reports gaps; it does not generate missing evidence.

## Surfaces

- `canonical_source`: source and range refs.
- `bundle_manifest`: artifact inventory and profile metadata.
- `agent_reading_pack`: navigation entry.
- `availability_freshness`: availability and freshness status.
- `citation_map`: stable citations and range support.
- `export_safety_report`: profile/export diagnostic from `RBV1-T009`.
- `code_task_evidence_matrix`: evidence rules from `RBAE-V1-T001`.
- `agent_change_plan`: pre-change plan from `RBAE-V1-T002`.
- `code_impact_map`: affected surfaces and gaps from `RBAE-V1-T003`.
- `relation_signals`: guarded relations from `RBV1-T014`.
- `graph_availability`: graph status from `RBV1-T015`.
- `python_symbol_index`: symbol/range data from `RBV1-T016`.
- `external_evaluation_plan`: expected CI, sidecar or review checks.

## Requirement values

- `required`: missing, stale or invalid evidence is `fail` unless not applicable.
- `recommended`: missing evidence is `warn`.
- `conditional`: required only when the target touches that surface.
- `not_applicable`: surface does not apply.

## Profile table

| Profile | Required emphasis | Recommended emphasis | Fail conditions |
|---|---|---|---|
| `code_bugfix` | target range, symbol index, impact map, change plan, expected check | relations, graph status, export report | range missing; stale source; no plausible check |
| `code_refactor` | symbol evidence, relation hints, behavior invariant, stop condition | graph status, export report | invariant absent; references stale/missing without gap |
| `code_feature` | requested behavior, candidate ranges, impact map, change plan, expected checks | relations, graph status, export report | no behavior owner; no expected check |
| `code_contract_change` | contract range, consumers when available, export report, impact map, change plan | graph status, symbol index | consumers missing without gap; compatibility effect lacks handoff |
| `code_test_repair` | tested behavior, tested source range, relation hints, non-claims | symbol index, impact map | no behavior claim; no source-of-truth range |
| `code_pr_review` | live PR head/diff, citations, impact map, CI/review state | graph status, symbol index | PR head/diff missing or stale; evidence not tied to PR |
| `code_sensitive_change` | export report, impact map, symbol/range evidence, risky boundary, external handoff | graph status | missing export report; unknown live state; no handoff |
| `code_docs_bound` | code/doc source of truth, freshness, affected docs | impact map, symbol index | docs used as authority without canonical code range |
| `code_tooling_change` | workflow/config range, expected checks, impact map | relations, graph status | affected job unknown; no validation check |
| `code_stateful_change` | state boundary, rollback/stop condition, live-state gap, external handoff | graph status, relation signals | live state unknown; rollback absent |

## Outcome model

- `pass`: required surfaces are available and freshness/live-state constraints are satisfied.
- `warn`: required surfaces pass, but recommended evidence is missing, not generated or profile-excluded.
- `fail`: required evidence is missing, stale, invalid, provenance-blocked, or needed live state is unknown.
- `not_applicable`: the profile or surface does not apply.

## Rules

1. Missing required evidence produces `fail`.
2. Stale required evidence produces `fail` unless an external live note supplies the missing fact.
3. Missing recommended evidence produces `warn`.
4. Unknown live state is `warn` for docs-only tasks and `fail` for PR review, sensitive, stateful, contract or runtime-impacting work.
5. Missing `export_safety_report` is `fail` whenever the selected RepoBrief profile or code task requires it.
6. Stale evidence may be shown as a gap, but must not be used as concrete impact evidence.
7. Resolving a profile must not create a snapshot, refresh a snapshot, mutate Git, run checks as a RepoBrief decision, open PRs or silently generate evidence.

## Minimal result shape

```json
{
  "schema_version": 1,
  "kind": "repobrief.code_required_reading_preflight",
  "task_kind": "code_contract_change",
  "status": "fail",
  "required": ["canonical_source", "bundle_manifest", "availability_freshness", "export_safety_report", "code_impact_map"],
  "recommended": ["graph_availability"],
  "missing_required": ["export_safety_report"],
  "warnings": [{"surface": "graph_availability", "status": "not_generated"}],
  "unknown_live_state": ["current PR head"],
  "non_claims": ["runtime_correctness", "test_sufficiency", "review_completeness", "merge_readiness", "agent_patch_quality_proven"]
}
```

## Acceptance mapping

- `rbae-v1-t004-profiles`: profile table maps task kinds to required and recommended RepoBrief surfaces.
- `rbae-v1-t004-preflight`: outcome model and rules define fail/warn behavior for missing evidence, stale freshness and unknown live state.
- `rbae-v1-t004-no-hidden-refresh`: rule 7 forbids refresh, Git mutation and implicit evidence generation.

## Does not establish

This contract does not establish runtime correctness, test sufficiency, complete dependency coverage, review completeness, merge readiness, patch correctness or agent patch quality.
