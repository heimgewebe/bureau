# Gemini proposal-only review lane v1

## Status

- Task: `CABINET-COHERENCE-FRONTIER-V1-T005`
- Tool: `bureau-gemini-review-lane`
- Authority: proposal only
- Lane activation: false by receipt; this tool does not dispatch or mutate anything

## Invocation shape

```bash
bureau-gemini-review-lane \
  --diff-file path/to/review.patch \
  --brief-file path/to/optional-brief.md \
  --output path/to/gemini-review.json \
  --json
```

The tool invokes Gemini as:

```bash
gemini --sandbox --print <bounded-review-prompt>
```

from an empty temporary directory.

## Input boundary

Allowed inputs are explicit, bounded, non-secret artifacts:

- one diff file;
- optionally one task brief file.

The tool does not search the repository and does not read `.env`, credential files, runtime state or private context. It rejects oversized artifacts and common secret-like patterns before Gemini is invoked.

## Output boundary

Gemini must return JSON with one of these statuses:

- `proposal`
- `blocked`
- `no_action`

If Gemini returns non-JSON or a schema-invalid object, the Bureau receipt becomes `blocked`. No patch is applied.

## Non-effects

Every receipt keeps these effect flags false:

- `writeAllowed`
- `pushAllowed`
- `mergeAllowed`
- `runtimeMutationAllowed`
- `credentialAccessAllowed`
- `dispatchAllowed`
- `queueMutationAllowed`
- `laneActivationAllowed`
- `effectPerformed`

Any suggestion remains evidence for Grabowski or human review only.
