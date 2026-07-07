# RBAE-V1-T007 Agent Code Workbench Promotion Gate

Status: closes `RBAE-V1-T007`.

## Purpose

This contract defines when Agent Code Workbench surfaces may become defaults for RepoBrief-assisted code work. Promotion requires measured improvement or clearer diagnostics. Existence of an artifact, schema, tool, CLI command or MCP resource is not enough.

The gate consumes outcome evaluations from `RBAE-V1-T006`, workbench surface definitions from `RBAE-V1-T005`, and task-profile expectations from earlier RBAE contracts. It remains a decision gate, not a proof of correctness.

## Candidate scope

A promotion candidate may be:

- a new workbench surface;
- a new default required-reading rule;
- a changed ranking or retrieval strategy;
- a new impact-map source;
- a new symbol/reference/test projection;
- a new CLI/MCP read-only endpoint.

Every candidate must declare the profiles it affects, the fallback path, expected benefits and known non-claims.

## Promotion criteria

Promotion may proceed only when all required criteria pass:

| Criterion | Required evidence |
|---|---|
| `measured_improvement` | Outcome evaluation shows better localization, evidence completeness, scope fit, check fit or clearer miss diagnostics. |
| `no_core_regression` | Central profiles do not regress unless explicitly deferred or dropped. |
| `bounded_surface` | Output is small, structured and read-only. |
| `freshness_visible` | Missing, stale, invalid and profile-excluded evidence remain visible. |
| `non_claims_present` | Result states what it does not establish. |
| `fallback_available` | Canonical source, citations and classical Required Reading remain usable. |
| `external_observation_linked` | CI, Patch Evaluation, review or operator observations are linked when used. |

Central profiles are `code_bugfix`, `code_refactor`, `code_feature`, `code_contract_change`, `code_pr_review` and `code_sensitive_change`.

## Regression control

For each central profile, compare candidate and baseline on:

- target file hit;
- target range overlap;
- required evidence coverage;
- stale evidence use;
- impact scope fit;
- unexpected file count;
- expected check coverage;
- miss taxonomy distribution.

A candidate fails the gate when a central profile regresses and there is no explicit `defer` or `drop` decision with a reason. Cosmetic improvement or more output volume must not override a regression.

## Gate outcomes

The gate returns one of:

- `promote`: candidate can become default for the declared profile/scope.
- `pilot`: candidate may be available behind explicit opt-in.
- `defer`: candidate is useful but missing evidence or external observations.
- `drop`: candidate should not continue in this form.
- `needs_more_data`: evaluation sample is too small or too narrow.

A `promote` outcome must include the fallback path and rollback trigger.

## Fallback rule

Fallback remains mandatory. If the promoted surface is missing, stale, invalid, profile-excluded or blocked by unavailable live state, the system must fall back to:

1. canonical source;
2. citation map and range refs when available;
3. existing Required Reading;
4. explicit gap reporting;
5. external handoff where live state is required.

Fallback must not silently regenerate evidence or mutate Git.

## Minimal record shape

```json
{
  "schema_version": 1,
  "kind": "repobrief.agent_workbench_promotion_gate",
  "candidate": "impact_map.get",
  "scope": ["code_bugfix", "code_refactor"],
  "status": "pilot",
  "criteria": {
    "measured_improvement": true,
    "no_core_regression": true,
    "bounded_surface": true,
    "freshness_visible": true,
    "non_claims_present": true,
    "fallback_available": true,
    "external_observation_linked": true
  },
  "profile_results": [
    {"profile": "code_bugfix", "decision": "promote", "reason": "localization improved without scope regression"},
    {"profile": "code_refactor", "decision": "pilot", "reason": "more samples needed for relation-heavy changes"}
  ],
  "fallback": ["canonical_source", "citation_map", "required_reading", "gap_report"],
  "rollback_triggers": ["central profile regression", "stale evidence hidden", "missing fallback"],
  "non_claims": ["runtime_correctness", "test_sufficiency", "review_completeness", "merge_readiness", "agent_patch_quality_proven"]
}
```

## Decision discipline

The gate may recommend promotion, pilot, defer or drop. It must not change defaults by itself, create pull requests, merge code, update runtime configuration or override human/operator review.

## Acceptance mapping

- `rbae-v1-t007-promotion-criteria`: satisfied by the promotion criteria and gate outcomes.
- `rbae-v1-t007-regression-control`: satisfied by central profile regression control and explicit defer/drop handling.
- `rbae-v1-t007-fallback`: satisfied by the fallback rule and required fallback fields in promoted records.

## Does not establish

This contract does not establish runtime correctness, test sufficiency, dependency completeness, review completeness, merge readiness, security correctness, patch correctness or agent patch quality.
