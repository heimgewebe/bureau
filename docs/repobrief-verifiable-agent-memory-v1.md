# RepoBrief Verifiable Agent Memory v1

## Purpose

Agent memory may store useful operational claims, but it must not become a truth oracle. This contract defines a bounded memory shape for durable agent recall when the remembered claim is tied to RepoBrief citation ids, source ranges or range refs.

The pattern is useful when an agent wants to recall a past repository fact, decision or operator rule without silently trusting free-form memory. Recall first revalidates the cited evidence. If the evidence is stale, missing, changed or unverifiable, the memory may be shown only as historical context with an explicit warning.

## Memory record shape

A memory claim record contains:

| Field | Required | Meaning |
| --- | --- | --- |
| `memory_id` | yes | Stable memory id. |
| `claim_text` | yes | The remembered claim, phrased as a claim rather than proof. |
| `repo` | yes | Repository name the claim was about. |
| `snapshot_stem` | yes | RepoBrief/rLens snapshot stem that produced the evidence. |
| `freshness_status` | yes | Freshness state at the time of storage. |
| `evidence[]` | yes | One or more RepoBrief citation ids, source ranges or range refs. |
| `last_revalidation` | optional | Latest recall check result. |
| `does_not_establish[]` | yes | Explicit non-claims. |

Each evidence entry records at least:

- `kind`: `repobrief_citation`, `repobrief_source_range` or `range_ref`;
- one address: `citation_id`, `range_ref`, or `source_path` with `start_line`/`end_line`;
- `expected_sha256` for the cited content or source range;
- `generated_at` and `max_age_hours` for freshness handling;
- `does_not_establish`, especially `claim_truth` and `repo_truth`.

The schema is `schemas/agent-memory-claim.v1.schema.json`.

## Recall check

Before presenting the memory as usable current context, the caller must supply current observations for the evidence ids. `bureau.verifiable_memory.evaluate_memory_recall` classifies each evidence entry as:

| Status | Meaning |
| --- | --- |
| `still_established` | Observation exists, hash matches and freshness has not expired. |
| `stale` | Hash still matches but the evidence is older than `max_age_hours`, or freshness cannot be checked. |
| `missing` | The cited evidence or observation is absent. |
| `changed` | Observation exists but `observed_sha256` differs from `expected_sha256`. |
| `unverifiable` | Required hash or time metadata is malformed or absent. |

Overall status is fail-closed: `changed` outranks `missing`, then `stale`, then `unverifiable`, then `still_established`. Only `still_established` sets `usable_for_context=true`.

## Boundary

This contract does not make memory source truth. A passing recall check means only that the stored evidence address is still present, hash-consistent and fresh enough. It does not establish runtime correctness, repo understanding, claim truth, merge readiness or policy authorization.

When recall fails, the agent may still mention the memory as historical context, but it must label the status and prefer live source evidence before acting.

## Acceptance mapping

- `rpu-v1-t015-memory-shape`: satisfied by the schema fields `claim_text`, evidence address, `snapshot_stem`, `expected_sha256`, `freshness_status` and `last_revalidation`.
- `rpu-v1-t015-recall-check`: satisfied by `evaluate_memory_recall`, which detects changed, stale, missing and unverifiable evidence before setting `usable_for_context`.
- `rpu-v1-t015-no-memory-truth`: satisfied by `presentable_as_source_truth=false` and explicit non-claims in the contract, schema and evaluator output.
