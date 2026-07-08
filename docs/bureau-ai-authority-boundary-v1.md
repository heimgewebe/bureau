# Bureau AI authority boundary v1

## Decision

Bureau core is deterministic-only. Local or external AI systems, including Ollama and Gemini, do not maintain Bureau and do not establish Bureau truth.

## Applies to

This boundary applies to:

- local models such as Ollama;
- external review or scout systems such as Gemini;
- copied LLM reviews;
- generated summaries, digests, proposals or task drafts.

## Allowed use

AI output may be preserved only as advisory material when the surrounding Bureau path remains deterministic:

- human-readable commentary;
- bounded proposal evidence;
- external review evidence after head, diff and prompt binding;
- semantic hints that are rechecked by Bureau, GitHub, CI, Grabowski or another source authority.

## Forbidden authority

AI output must not directly or indirectly establish:

- queue truth;
- registry truth;
- task readiness;
- claim truth;
- task verification;
- task completion;
- merge readiness;
- CI correctness;
- runtime correctness;
- dispatch authority;
- source-system authority.

## Ollama decision

The current local Ollama inventory is not accepted as a Bureau secretary surface. The available models may be useful for ad hoc local explanation, but they are outside Bureau's official maintenance path until a later task supplies a benchmark suite, deterministic validator and explicit no-mutation contract.

Do not add an Ollama Bureau secretary, daemon, queue assistant, scout lane or task-import path as part of the Bureau core.

## Deterministic substitute

The intended substitute for a secretary is deterministic status projection and guards:

1. registry and queue validation;
2. per-repository active-ball projection;
3. open-PR task binding checks;
4. closeout and verification evidence checks;
5. read-only status reports with explicit unknowns and blocked reasons.

## Relation to Cabinet Gemini lanes

Cabinet Gemini lanes remain proposal-only and effect-free. They are not a precedent for Bureau maintenance by AI. Cabinet may use bounded semantic scans as radar; Bureau imports or acts only through deterministic review, schemas, evidence, queue rules and receipts.

## Does not establish

This document does not establish runtime correctness, model quality, semantic scan quality, merge readiness or task completion. It records the authority boundary only.
