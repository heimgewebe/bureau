# Reposkop checkout evidence references v1

Bureau receipts may carry a `reposkop_checkout_ref` object inside their existing free-form `evidence` object. The reference binds repository-scoped execution evidence to Reposkop's canonical local checkout identity and, when available, its post-effect transition and continuity artifacts.

The reference is validated by `schemas/reposkop-checkout-evidence-ref.v1.schema.json`.

## Boundary

This reference establishes only which local checkout identity and transition accompanied an operation. It does not establish:

- task completion or verification;
- claim, queue, lease or ownership truth;
- pull-request or CI truth;
- remote freshness;
- effect authorization or effect success.

Bureau remains authoritative for task lifecycle. Reposkop remains authoritative for local checkout identity, transition and continuity.

Consequently, `reposkop_checkout_ref` cannot be the sole acceptance criterion for `complete_run`. Every run that records it must also satisfy at least one independent Bureau-owned acceptance criterion. This applies to every continuity state: even `intact` or `explainable_drift` proves repository continuity, not task completion; `pre_effect_only`, `identity_break` and `inconclusive` are never successful completion signals.

Idempotent replay revalidates the stored receipt schema, its self-declared digest and the database digest before materializing or returning it.

## Pre-effect reference

A task may record only a bound starting observation:

```json
{
  "schema_version": 1,
  "producer": "reposkop",
  "repository": "heimgewebe/example",
  "purpose": "bureau-task-execution",
  "pre_observation_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "post_observation_sha256": null,
  "repository_identity_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "checkout_identity_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "transition_sha256": null,
  "continuity_sha256": null,
  "continuity_state": "pre_effect_only",
  "does_not_establish": ["task_completion", "effect_authorization"]
}
```

## Completed transition reference

A repository effect receipt should bind pre- and post-observation, transition and continuity digests. `identity_break` and `inconclusive` remain admissible as truthful evidence, but must not be interpreted as successful verification.

Consumers should reject malformed references rather than silently dropping them. The optional nature of the field means tasks that do not require repository identity remain unaffected.
