# Bureau Approval Path v1

## Purpose

Bureau separates four phases:

1. **Observation**: read current registry, runtime, GitHub, CI, RepoBrief, or queue state.
2. **Planning**: create a proposal, dry-run report, handoff, or candidate task without effect.
3. **Approval**: bind a named decision to a task and effect scope.
4. **Execution**: mutate a repository, import external evidence, dispatch an agent, or touch runtime.

The approval path is not a merge verdict and not proof that a task is correct. It is only a deterministic gate for whether a requested effect has enough recorded authority to proceed.

## Effect classes

| Effect class | Examples | Required approval | Notes |
| --- | --- | --- | --- |
| `read_observation` | status, doctor, frontier, GitHub observation | `none` | Must not mutate registry or runtime. |
| `planning_proposal` | dry-run plan, candidate task draft | `none` | Proposal only; no queue or repo mutation. |
| `registry_mutation` | queue edit, Bureau task update, registry write | `reviewed_plan` | Needs a reviewed plan or embedded approval. |
| `task_creation_from_external_evidence` | creating Bureau tasks from Cabinet, Steuerboard, Gemini, or other imported evidence | `reviewed_plan` | External evidence is input, not authority. |
| `source_import` | source sync, fetch, import into Bureau from external state | `operator` | Import can change registry meaning, so operator approval is required. |
| `repository_mutation` | branch, commit, push, PR mutation in a target repo | `operator` | The approval must match the task/effect scope. |
| `agent_dispatch` | starting a Grabowski task or other external worker | `operator` | Dispatch is an effect even when the worker later performs its own gates. |
| `runtime_mutation` | service restart, deploy, systemd change | `operator` | Runtime evidence is required separately. |
| `privileged_mutation` | sudo/root/power broker effect | `privileged_operator` | Requires the strongest non-prohibited approval. |
| `prohibited` | policy-prohibited action | impossible | Fails closed even with approval evidence. |

## Approval evidence shape

Minimal approval evidence:

```json
{
  "schema_version": 1,
  "task_id": "BUR-2026-003-T004",
  "approved": true,
  "decision": "approve",
  "level": "operator",
  "reviewer": "operator",
  "scope": "task"
}
```

Accepted approval levels, from weaker to stronger:

1. `none`
2. `reviewed_plan`
3. `operator`
4. `privileged_operator`

`scope` may be `task`, one effect class, or a list of effect classes. For multi-effect requests, the scope must cover every requested/inferred effect class; if scope is too narrow, the gate blocks.

## Fail-closed rules

- Unknown requested effects are rejected.
- Missing approval blocks every effect above `none`.
- A lower approval level never satisfies a stronger effect class.
- `approved` must be `true`.
- If `task_id` is present on the task and approval, they must match.
- Approval must include a reviewer and must cover the requested effect scope.
- `prohibited` cannot be approved.

## Operator Relay compatibility

The approval evaluator is pure and bounded. It returns only a decision object and never performs Git, queue, runtime, dispatch, or import mutation. Operator Relay can therefore use it before a handoff without turning the check itself into an effect.

## What this does not establish

Approval path v1 does **not** establish:

- task correctness;
- runtime correctness;
- review completeness;
- merge readiness;
- operator intent beyond the approved scope.

Those remain separate evidence gates.
