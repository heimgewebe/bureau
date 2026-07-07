# RepoBrief Agent Change Plan Contract v1

Status: closes Bureau task `RBAE-V1-T002`.

## Purpose

The Agent Change Plan is the handoff object between RepoBrief evidence and any
future mutable evaluation surface. It lets a coding agent state what it intends
to change, why the evidence points there, what it still does not know, and which
external checks should be run before anyone treats the patch as plausible.

It is deliberately not a patch, not a command plan, not a test result, and not a
merge or review verdict. It is a bounded planning contract for agents.

## Authority boundary

RepoBrief and the Agent Workbench may help an agent produce or validate an Agent
Change Plan from deterministic, read-only evidence surfaces:

- canonical source ranges;
- citation maps;
- required-reading results;
- code task evidence matrix results;
- source citation projections;
- static symbol, reference, relation and graph hints when available;
- freshness, availability and health diagnostics;
- external Patch Evaluation artifacts as external observations only.

They must not:

- apply the plan as a patch;
- create or modify files in the target repository;
- create branches, worktrees or pull requests;
- run shell commands, tests, linters, formatters, migrations or sandboxes;
- read secrets;
- infer that the intended patch is correct;
- infer that tests would pass;
- infer that CI or review would approve;
- infer merge readiness.

Patch application and execution belong to external surfaces such as an isolated
Patch Evaluation Sidecar, CI, GitHub PR checks, explicit operator commands or
human review.

## Contract shape

A strict Agent Change Plan should contain these fields. Field names are contract
names for future schema/CLI/MCP work; this document does not implement a parser.

| Field | Required | Meaning |
|---|---:|---|
| `schema_version` | yes | Contract version. This document defines version `1`. |
| `kind` | yes | Constant such as `repobrief.agent_change_plan`. |
| `task_kind` | yes | One profile from the code task evidence matrix, such as `code_bugfix`, `code_refactor`, `code_feature`, `code_contract_change`, `code_pr_review` or `code_security_sensitive`. |
| `target_behavior` | yes | The behaviour, contract, review claim or documentation alignment the agent intends to change or evaluate. |
| `evidence_basis` | yes | Cited RepoBrief/source ranges and other read-only evidence the agent inspected. |
| `candidate_changes` | yes | Files, symbols or contracts likely to change, with cited rationale. No patch content is required. |
| `impact_hypothesis` | yes | Expected affected callers, tests, docs, schemas, workflows, runtime boundaries or operator surfaces. |
| `expected_external_checks` | yes | Tests, CI jobs, static checks, review steps, sidecar commands or manual checks that should run outside RepoBrief. |
| `live_gaps` | yes | Current state that a snapshot cannot establish, such as PR head, dirty tree, CI state, runtime state, secrets or deployed version. |
| `stop_conditions` | yes | Conditions that must stop patching or require escalation. |
| `handoff_targets` | yes | External surfaces allowed to consume the plan, for example Patch Evaluation Sidecar, CI, GitHub PR review or human operator. |
| `non_claims` | yes | Assertions the plan does not establish. |
| `confidence` | no | Optional calibrated confidence or risk level, without becoming a verdict. |
| `notes` | no | Optional explanatory notes for a downstream agent or reviewer. |

## Evidence basis requirements

Every concrete code claim in `evidence_basis` or `candidate_changes` must be
bound to a source range, citation, PR diff range, or explicit live observation.
A generic statement such as "the function probably handles this" is not enough.

For each evidence item, the plan should record:

- evidence type: canonical range, PR diff, symbol hint, relation hint, test hint,
  health/freshness diagnostic, patch-evaluation observation, CI observation, or
  human/operator observation;
- source identifier or path;
- range/citation when available;
- freshness or live-state status;
- whether it is required or supporting evidence;
- what it does not establish.

When evidence is missing, the plan must say whether work is blocked, degraded to
an investigation plan, or safe only as a provisional patch hypothesis.

## Candidate change requirements

A candidate change entry should identify:

- target file or contract surface;
- target symbol or section when known;
- reason for touching it;
- related evidence item ids;
- expected type of change: implementation, test, docs, schema, CLI/MCP/API,
  workflow, config or runtime/runbook;
- whether the change is minimal or expands scope;
- rollback or revert consideration when applicable.

The plan must avoid writing the actual patch as the authoritative output. Patch
content belongs to a separate patch artifact or external patch application step.

## Impact hypothesis requirements

The impact hypothesis is a risk map, not a completeness proof. It should list
what is likely affected and what remains unknown.

Typical impact surfaces:

- callers and references;
- tests and fixtures;
- contracts and schemas;
- CLI/MCP/API entrypoints;
- docs, runbooks and generated artifacts;
- workflows and CI jobs;
- runtime services, migrations, state roots and operator procedures;
- security-sensitive input, path, secret, auth, permission or network surfaces.

If an impact surface is unavailable, stale or not generated, the plan must say
so explicitly.

## Expected external checks

The plan may recommend checks but cannot run them or treat them as proof.

Each expected check should include:

- command or check name when known;
- owner surface: Patch Evaluation Sidecar, CI, GitHub, human/operator, security
  review or runtime smoke;
- reason the check matters;
- expected observation, not expected approval;
- what failure would mean;
- what passing still does not establish.

A passing external check may become evidence for a narrower observation, but it
never establishes general correctness, test sufficiency, security correctness,
review completeness, regression absence or merge readiness by itself.

## Live gap rules

A plan must include `live_gaps` when any of these are unknown:

- current branch or head SHA;
- dirty working tree or untracked files;
- open PRs touching the same surface;
- PR diff, review state, mergeability or CI state;
- runtime deployment or service state;
- database, migration, state-root or backup status;
- secret or config availability;
- generated artifact freshness;
- dependency or security advisory currency.

If a live gap is material to the task, the plan must either block patching or
mark the patch as provisional pending external evidence.

## Stop conditions

A plan must define when an agent should stop or escalate. Examples:

- required source range or PR diff is missing;
- evidence freshness is stale and the task depends on current state;
- impact surface includes security, secrets, migrations, production runtime or
  destructive state changes;
- tests/checks cannot be identified for a behavioural change;
- expected change spans unrelated subsystems;
- available evidence contradicts the target behaviour;
- external Patch Evaluation or CI reports failure outside the agent's current
  scope;
- review/human/operator authority is required.

Stop means no patch should be applied by an automated downstream surface without
new evidence or explicit operator decision.

## Handoff to Patch Evaluation and CI

The Agent Change Plan can be handed to external systems as input evidence. A
Patch Evaluation Sidecar may use it to create an isolated worktree, apply a
separate patch, run configured commands and emit a patch-evaluation artifact.
CI or GitHub PR review may use it to understand intent and required checks.

The plan itself must remain read-only and non-authoritative. It may say what
should be evaluated; it must not claim the evaluation passed.

## Minimal example

```json
{
  "schema_version": 1,
  "kind": "repobrief.agent_change_plan",
  "task_kind": "code_bugfix",
  "target_behavior": "Resolve the observed CLI alias failure without changing snapshot semantics.",
  "evidence_basis": [
    {
      "id": "ev1",
      "type": "canonical_range",
      "path": "src/example.py",
      "range": "L10-L42",
      "required": true,
      "freshness": "snapshot_known_stale",
      "does_not_establish": ["current_worktree", "runtime_correctness"]
    }
  ],
  "candidate_changes": [
    {
      "path": "src/example.py",
      "symbol": "build_alias_command",
      "reason": "Evidence ev1 shows alias dispatch is centralized here.",
      "evidence": ["ev1"],
      "change_type": "implementation"
    }
  ],
  "impact_hypothesis": {
    "likely_tests": ["tests/test_example_alias.py"],
    "contracts": ["CLI command output"],
    "unknown": ["current branch head", "open PR collisions"]
  },
  "expected_external_checks": [
    {
      "surface": "Patch Evaluation Sidecar",
      "check": "pytest tests/test_example_alias.py",
      "reason": "Narrow alias behaviour check",
      "passing_does_not_establish": ["test_sufficiency", "merge_readiness"]
    }
  ],
  "live_gaps": ["current branch head", "dirty working tree", "CI state"],
  "stop_conditions": ["source range missing", "open PR changes same alias file"],
  "handoff_targets": ["patch_evaluation_sidecar", "github_pr_review"],
  "non_claims": [
    "correctness",
    "test_sufficiency",
    "runtime_behavior",
    "security_correctness",
    "merge_readiness",
    "review_completeness",
    "regression_absence"
  ]
}
```

## Acceptance mapping

- `rbae-v1-t002-contract`: satisfied by the contract shape, evidence basis,
  candidate change, impact hypothesis, expected checks, live gaps and stop
  condition sections.
- `rbae-v1-t002-no-execution`: satisfied by the authority boundary and explicit
  prohibition on patch application, shell/test execution, Git mutation, secret
  access, PR creation and merge/review verdicts.
- `rbae-v1-t002-evaluation-handoff`: satisfied by the handoff section and the
  rule that Patch Evaluation/CI consume the plan as input evidence only.

## Does not establish

This contract does not implement a parser, add a Lenskit schema, apply patches,
run tests, prove that generated plans are complete, prove patch correctness,
prove runtime correctness, prove security correctness, prove test sufficiency,
prove review completeness, authorize merges or prove agent patch quality.
