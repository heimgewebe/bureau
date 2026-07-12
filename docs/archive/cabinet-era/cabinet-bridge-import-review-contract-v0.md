# Cabinet Bridge Import Review Contract v0

This is a design contract for the Cabinet-to-Bureau bridge after a bridge receipt exists.

It does not add import behavior.

## Evidence chain

```text
bridge-probe-report.json
-> bridge-preview.json
-> bridge-review.json
-> bridge-receipt.json
```

## Required receipt state

An import-review step must start from a receipt with these fields:

- `kind == "cabinet_bridge_review_receipt"`
- `status == "review_recorded"`
- `importAllowed == false`
- `importReviewRequired == true`
- `dispatchAllowed == false`
- `queueMutationAllowed == false`
- `taskCreationAllowed == false`

## Required import-review inputs

- current Cabinet bridge policy
- exact bridge probe report
- exact bridge preview
- exact bridge review gate
- exact bridge receipt
- Cabinet commit SHA
- Bureau commit SHA
- explicit target path
- explicit write surface
- reviewer identity distinct from `cabinet-ci-review-gate`

## Non-effects

This contract does not create tasks, mutate queues, write Bureau registry files, dispatch work, or run runtime actions.

## Stop conditions

Stop if any required artifact is missing, any effect flag is true, `importReviewRequired` is missing or false, the target path is implicit, or the write surface is implicit.

## Organ roles

Cabinet owns the source claim and evidence context.

Bureau owns interpretation of this contract and any later Bureau-side design.

GitHub CI can produce technical evidence, but it is not the import reviewer.
