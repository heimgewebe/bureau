# Gemini proposal lane preflight v1

## Status

- Task: `CABINET-COHERENCE-FRONTIER-V1-T008`
- Mode: preflight only
- Lane state after this slice: blocked until auth/quota and policy review is explicitly recorded
- Tool: `bureau-gemini-preflight`

## Boundary

The preflight may inspect only executable metadata such as `--version` and `--help` output. It must not send repository content, prompts, credentials, environment files, deploy data or private runtime context to Gemini.

## Allowed future inputs

Gemini may only receive explicitly selected, bounded and non-secret review context:

- public or already review-approved PR diffs;
- bounded non-secret task briefs;
- schema-valid Cabinet Frontier candidates;
- sanitized prompts with explicit forbidden changes.

## Excluded context

Gemini must not receive:

- credentials, tokens or keys;
- `.env` contents;
- private runtime data;
- deploy-only material;
- unreviewed private context;
- direct repository mutation authority.

## Required before lane activation

A later lane must still record:

1. Auth/quota state without leaking account details.
2. Exact non-interactive invocation shape.
3. Output capture path.
4. Sandbox or permission mode.
5. Proof that Gemini cannot write, push, merge, mutate runtime, dispatch agents or mutate Bureau queue.

Until then, Gemini is not schedulable/einplanbar.
