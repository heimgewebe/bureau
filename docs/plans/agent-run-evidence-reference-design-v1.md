# Agent Run Evidence Reference Design v1

Status: draft
Scope: read-only Bureau references to agent-run evidence

## Context

Chronik and Grabowski now provide local Agent Ledger evidence: task-local opt-in, preview, run view and local review summaries. Cabinet has accepted the routing policy and has assessed Bureau as a future coordination organ.

Bureau already owns task candidates, envelopes, receipts and run coordination. It should not become the executor for local runtime state.

## Decision

Bureau may later reference Agent Ledger evidence only as small read-only metadata.

Bureau must not:

- trigger Grabowski tasks;
- move local outbox data;
- call Chronik ingestion;
- store full local runtime dumps;
- infer task success from repo-level summaries alone.

## Reference shape

A future reference should be a small object embedded in a task, receipt or review record.

Required fields:

| field | meaning |
| --- | --- |
| `schema` | constant such as `bureau.agent-run-evidence-ref.v1` |
| `kind` | `local_preview`, `chronik_event`, or `manual_report` |
| `source_organ` | `grabowski`, `chronik`, or `cabinet` |
| `subject_repo` | repository being described |
| `summary_result` | `completed`, `blocked`, `mixed`, or `unknown` |
| `evidence_ref` | compact reference, not copied evidence |
| `reviewed_at` | timestamp of Bureau review |
| `does_not_establish` | explicit limits |

Optional fields:

| field | meaning |
| --- | --- |
| `run_id` | one exact run id, if known |
| `task_id` | one exact task id, if known |
| `preview_root_name` | basename of a local preview root |
| `chronik_event_id` | only after a separate manual gate |
| `pr` | related PR |
| `commit` | related commit |

## Mandatory limits

Every reference must state what it does not prove. Common entries:

- `does_not_authorize_execution`
- `does_not_trigger_chronik`
- `does_not_prove_current_runtime_state`
- `does_not_replace_receipt_validation`

## Placement order

1. schema-only contract with fixtures;
2. receipt `external` object for reviewed historical evidence;
3. task acceptance metadata;
4. dashboard summary derived from existing receipts.

## Gates before implementation

1. Cabinet policy permits the task class.
2. Bureau schema change is reviewed as read-only.
3. Tests prove no action is triggered by the reference.
4. Receipt verification remains independent.
5. Runtime evidence is summarized, not copied.
6. Repo-level and run-level summaries remain distinguishable.
7. PR self-review confirms no control-plane coupling.

## Decision state

Do not implement live Bureau references yet.

The next admissible slice is a schema-only `agent-run-evidence-ref.v1` contract with fixtures and tests.
