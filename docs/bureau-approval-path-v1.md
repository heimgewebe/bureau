# Bureau Approval Path v1

Status: closes `BUR-2026-003-T004`.

## Decision

Bureau separates observation from effect. A command may read, project, rank or dry-run without approval. A command that mutates source state, imports external evidence into Bureau, dispatches an agent, creates a Bureau task from external evidence, mutates the queue or changes runtime state must pass an explicit approval gate before the effect.

## Approval classes

| Action class | Examples | Required approval level | Default without approval |
|---|---|---|---|
| `read_only_observation` | registry status, frontier, projection, dry-run diagnostics | none | allowed |
| `proposal_preview` | Cabinet preview, design-only candidate rendering | none for preview; explicit preview command may still require `--approve` | allowed only where caller remains effect-free |
| `repository_mutation` | branch/write/commit/push/merge, worktree writes used for a task | `operator` | fail closed |
| `source_import` | reviewed Cabinet Frontier import into Bureau registry | `reviewed_receipt` | fail closed |
| `agent_dispatch` | starting an external Grabowski task from a Bureau run | `operator` | fail closed |
| `task_creation_from_external_evidence` | turning Cabinet/Gemini/source candidates into Bureau task material | `operator` | fail closed |
| `registry_mutation` | generic or legacy Bureau Registry mutation not covered by a narrower typed action | `reviewed_plan` | fail closed |
| `queue_mutation` | applying a queue-reconcile plan | `reviewed_plan` | fail closed |
| `runtime_mutation` | deploy, restart, service repair, migration | `operator` | fail closed |

Approval levels are typed capabilities, not a pure numeric ladder. `reviewed_plan` and `reviewed_receipt` are intentionally not interchangeable. `break_glass` remains an exceptional escalation capability and may satisfy lower gates only where the action rule explicitly allows it. Routine runtime work uses `operator`; a trusted-owner mandate may supply that operator authority without a separate human break-glass round-trip.

## Enforcement contract

The implementation is `bureau.approval`.

- `approval_decision(action_class, evidence, expected_reference=...)` returns a deterministic allow/block object.
- `require_approval(action_class, evidence, expected_reference=...)` raises before any effect when the action is missing approval, has an unknown action class, has `approved=false`, has insufficient level, or carries the wrong source reference.
- `task_approval_contract(task)` infers the conservative task-level action class when no explicit `execution.approval` is declared.

Unknown effectful action classes fail closed. This is intentional: a new effect must be classified before it can be automated. Approval records may carry `reference`, `task_id`, and `scope`; when callers provide expected values, mismatches fail closed. In mixed checks, read-only actions remain visible in the decision object but do not increase the required approval level.

## Current integration points

- Agent dispatch through `checkout-next --dispatch` records an `agent_dispatch` approval decision tied to the explicit CLI flag and current run id.
- Cabinet bridge and Cabinet frontier previews record `task_creation_from_external_evidence` approval evidence when `--approve` is supplied and bind it to the proposed task id.
- Operator-intake Candidate→TaskSpec preview remains effect-free and non-authorizing. Publish accepts `task_creation_from_external_evidence` only after Bureau has live-validated Grabowski's server-owned publication Authority against the exact proposal SHA, publishing task id, operation, action class, capability and Bureau phase. Public/caller-provided resource metadata cannot mint that Authority.
- Generic `registry_mutation` remains bound to `reviewed_plan`; the narrower Candidate→TaskSpec authority does not satisfy that gate.
- Reviewed Cabinet Frontier import uses the reviewed receipt as `source_import` approval when `--apply` writes a task file and binds approval to the receipt path.
- Queue reconcile apply uses the reviewed plan as `queue_mutation` approval and binds approval to the reviewed plan path.

## Operator Relay compatibility

This path does not ask the user to become a shell executor. Approval is a recorded input to a bounded command or reviewed artifact. The operator loop remains: inspect state, choose a safe action, run bounded tools, preserve receipts, then report evidence.

## Does not establish

This document does not grant automatic merge, automatic runtime repair, automatic task verification, automatic queue repair, broad source import, or dispatch authority from AI output. AI and external systems can supply advisory evidence only; Bureau still requires deterministic gates and source-bound receipts. Where an action class requires `reviewed_plan` or `reviewed_receipt`, that reviewed artifact remains mandatory; the narrow Candidate→TaskSpec path instead uses its explicitly typed, server-owned operator authority.
