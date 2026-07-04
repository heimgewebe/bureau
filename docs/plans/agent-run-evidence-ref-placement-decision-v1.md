# Agent Run Evidence Reference Placement Decision v1

Status: accepted
Scope: placement decision for `bureau.agent-run-evidence-ref.v1`

## Context

Bureau now has a schema-only contract for Agent Run evidence references. The contract validates compact references to local previews, Chronik events and manual reports. It does not place those references in live tasks or receipts.

Bureau already has three relevant surfaces:

- task documents;
- receipt documents;
- review or plan documents.

The next decision is where the reference may appear first.

## Options

| Option | Placement | Benefit | Risk | Decision |
| --- | --- | --- | --- | --- |
| A | Review or plan document only | safest, no runtime meaning | lower operational usefulness | allowed now |
| B | `receipt.external` | fits reviewed historical evidence | could be mistaken for receipt proof | allowed later with tests |
| C | task acceptance metadata | helps task planning | risks pre-claiming evidence before execution | defer |
| D | queue or dashboard summary | visible overview | risks control-plane coupling | reject now |
| E | direct Chronik or Grabowski binding | automated freshness | creates side effects and dependency | reject |

## Decision

Choose **A now**: use the reference contract only in review or plan documents.

Allow **B later** only after a separate schema change proves that `receipt.external` remains supplemental and cannot replace receipt validation.

Defer **C**. A task must not treat an Agent Run evidence reference as acceptance evidence before a reviewed run exists.

Reject **D** and **E** for now.

## Rules

1. `agent-run-evidence-ref.v1` is not execution permission.
2. It is not a replacement for Bureau receipt validation.
3. It must not trigger Grabowski, Chronik or any downstream action.
4. Repo-level summaries must not hide run-level blocked rows.
5. Chronik event references are valid only after a separate manual movement gate.
6. Local preview references must remain compact references, not copied local runtime data.

## Next admissible implementation

A future implementation may add a fixture-backed review document that embeds one valid `agent-run-evidence-ref.v1` object.

Do not add live task or receipt placement until that review-document usage has been exercised and reviewed.
