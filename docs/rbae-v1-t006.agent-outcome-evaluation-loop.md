# RBAE-V1-T006 Agent Outcome Evaluation Loop

Status: closes `RBAE-V1-T006`.

## Purpose

This contract defines how to evaluate whether RepoBrief and its Agent Workbench actually improve agent code handling. The loop measures outcomes; it does not prove correctness.

The loop consumes existing RepoBrief evidence, workbench outputs, Patch Evaluation/CI/review observations and final task outcomes. It compares the agent's chosen path against expected evidence and records misses.

## Evaluation axes

| Axis | Question | Example signals |
|---|---|---|
| `localization` | Did the agent find the right files, symbols and ranges? | target path hit, range overlap, symbol match, citation use |
| `evidence_completeness` | Did the agent use required evidence for the task profile? | required-reading pass/warn/fail, missing required surfaces, stale evidence |
| `patch_scope` | Did the change stay within the expected impact map? | changed files vs impact surfaces, unrelated file drift, generated-file drift |
| `check_fit` | Were the selected checks relevant to the change? | expected checks present, missing targeted tests, overbroad or irrelevant checks |
| `external_observation` | What did CI, Patch Evaluation, review or operator evidence report? | pass/fail/warn, reviewer findings, CI job result, sidecar diagnostics |
| `miss_taxonomy` | Why did the agent miss? | localization miss, stale evidence, missing live state, wrong contract, overbroad patch |

## Outcome record shape

A single evaluation record should include:

- `schema_version`;
- `kind`: `repobrief.agent_outcome_evaluation`;
- `task_kind`;
- `input_refs`: task, bundle, change plan, impact map and workbench call ids;
- `agent_path`: files/ranges/symbols/checks selected by the agent;
- `expected_path`: files/ranges/symbols/checks expected from evidence;
- `observations`: external CI, sidecar, review or operator results;
- `metrics`: bounded numeric or boolean metrics;
- `miss_taxonomy`: structured miss categories and examples;
- `non_claims`.

## Metrics

Required metrics:

- `target_file_hit`: whether expected target files were selected.
- `target_range_overlap`: coarse overlap of selected and expected ranges.
- `symbol_hit`: whether expected symbols were selected when symbol evidence exists.
- `required_evidence_coverage`: fraction of required evidence surfaces read or cited.
- `stale_evidence_used`: whether stale evidence supported a concrete claim.
- `impact_scope_fit`: changed files within expected impact surfaces.
- `unexpected_file_count`: files changed outside expected scope.
- `expected_check_coverage`: fraction of expected checks selected or run externally.
- `external_failure_count`: number of failing external observations.
- `self_authored_check_only`: whether the only green checks were authored by the same agent/change.

Optional metrics may include per-profile recall, MRR, range coverage and review-specific deltas from retrieval evaluation. These stay diagnostic.

## Miss taxonomy

Use these miss categories:

- `localization_miss`: wrong file, range or symbol.
- `evidence_gap`: required evidence missing or not generated.
- `freshness_miss`: stale evidence used as if current.
- `live_state_gap`: PR head, CI state or runtime state unavailable.
- `scope_creep`: patch changed files outside expected impact.
- `check_miss`: relevant checks missing or irrelevant checks substituted.
- `contract_miss`: schema/API/MCP/CLI contract impact missed.
- `boundary_miss`: read-only, profile, export or safety boundary missed.
- `self_proof`: agent-authored tests or passing checks treated as independent proof.
- `external_regression`: CI, sidecar, review or operator observation reports a failure.

## External observations

Patch Evaluation, CI, review and operator observations are external evidence. They may raise confidence, identify misses or block promotion. They do not prove runtime correctness, security correctness, review completeness or merge readiness.

Each observation should record:

- source type;
- source id or URL when available;
- status;
- head/diff/check identity when relevant;
- whether the observation is independent from the agent-authored change;
- what the observation does not establish.

## No self-proof rule

An agent must not treat its own proposed tests, local green checks or generated evaluation notes as independent correctness proof. They may be evidence that a path was attempted. Independent confidence requires an external observation, pre-existing test, reviewer finding, CI result, Patch Evaluation sidecar result or operator-provided evidence.

## Feedback loop

The loop feeds future planning by recording:

1. which workbench surfaces were useful;
2. which required evidence was missing or stale;
3. which files/ranges were wrongly localized;
4. which checks were missing or overused;
5. which miss categories repeated;
6. whether follow-up tasks should adjust required reading, impact maps, retrieval evaluation or workbench ergonomics.

The loop may propose a Bureau task or planning note, but it must not auto-promote a model, change defaults, create PRs or merge work.

## Minimal example

```json
{
  "schema_version": 1,
  "kind": "repobrief.agent_outcome_evaluation",
  "task_kind": "code_bugfix",
  "status": "warn",
  "metrics": {
    "target_file_hit": true,
    "target_range_overlap": 0.5,
    "required_evidence_coverage": 0.75,
    "impact_scope_fit": false,
    "unexpected_file_count": 1,
    "expected_check_coverage": 0.5,
    "self_authored_check_only": true
  },
  "miss_taxonomy": ["scope_creep", "self_proof", "check_miss"],
  "observations": [
    {"source_type": "ci", "status": "pass", "independent": true},
    {"source_type": "patch_evaluation_sidecar", "status": "warn", "independent": true}
  ],
  "non_claims": ["runtime_correctness", "test_sufficiency", "review_completeness", "merge_readiness"]
}
```

## Acceptance mapping

- `rbae-v1-t006-metrics`: satisfied by the evaluation axes, metrics and miss taxonomy.
- `rbae-v1-t006-no-self-proof`: satisfied by the no self-proof rule.
- `rbae-v1-t006-sidecar-link`: satisfied by the external observations section and non-claims.

## Does not establish

This contract does not establish runtime correctness, test sufficiency, retrieval completeness, review completeness, merge readiness, security correctness, patch correctness or agent patch quality.
