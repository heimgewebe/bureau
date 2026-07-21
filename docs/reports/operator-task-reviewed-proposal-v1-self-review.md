# Operator task reviewed proposal v1 — Self-Review

## Binding

- Base: `b9a7263c50daaf57b8bda33d07c7be8b6dab80a7`
- Reviewed head: `2ed610cb0c5c8e8c27430e01ff06d65ba271bcfc`
- Reviewed tree: `63cd3f78d21c5aef80553e904afc5c7a8df94e8c`
- Reviewed binary full-index diff SHA-256: `59a5a4c462f3fbc4606ac8ad82770c91cc6f259e7d5e63fbe7106acfd39efa35`
- Verdict: **PASS**

## Reviewed scope

1. Add the public Bureau operation `operator-task-review` and the domain operation `review_task_proposal(...)`.
2. Bind one review to the exact unsigned proposal digest and reject hash drift, unresolved fields and conflicting replay.
3. Generate the productive review timestamp only inside Bureau; neither CLI nor domain callers can provide it.
4. Produce `reviewed_plan` approval evidence without mutating Registry, Queue or publication truth.
5. Use a Linux `renameat2(RENAME_EXCHANGE)` compare-and-swap with bounded no-follow regular-file reads, directory-identity checks and typed ambiguity outcomes.
6. Harden proposal reads used by publication preview and publication effect against symlinks, oversized files and parent-directory replacement.
7. Extend the operation and lease contracts, documentation and end-to-end tests.

## Dialectical review

### Benefit

The new operation removes an untyped manual gap between proposal creation and publication preview. Review identity, proposal identity, effect state, retryability and required readback are now explicit machine-readable outputs. Exact replay is idempotent, while materially different replay fails closed.

### Counter-risk

Review is a mutation of a local proposal artifact. A naïve in-place rewrite could lose concurrent changes, follow a symlink or report success after an uncertain partial effect. The implementation therefore binds the source inode and bytes, swaps atomically, verifies the displaced preimage and the installed postimage, and reports ambiguity rather than inventing success after the exchange starts.

### Alternative axis

A portable write-to-temporary-and-rename implementation would work on more operating systems but cannot preserve both names for a deterministic compare-and-swap and rollback check. The selected Linux-specific exchange gives stronger local atomicity. This is valid for the current Bureau runtime; portability remains an explicit limitation rather than a hidden claim.

## Findings and corrections during review

- Removed public CLI `--reviewed-at` and the domain `reviewed_at` parameter. Productive time now comes only from `legacy.utc_now()`.
- Reduced idempotent replay identity to reviewer plus reviewed proposal digest. The internally generated timestamp is evidence, not caller-controlled replay input.
- Confirmed the remaining fixed review timestamp is test-fixture data and does not simulate a public timestamp parameter.
- Confirmed the proposal digest is recomputed from unsigned content and checked both against stored proposal integrity and the caller's expected digest.
- Confirmed unresolved fields prevent review and an existing review by another reviewer or for another digest fails closed.
- Confirmed review results explicitly do not establish Registry mutation, Queue mutation or publication effect.
- Confirmed the atomic path detects source-byte or inode drift, restores foreign bytes after a detected conflict, and requires readback when the effect may already have started.
- Confirmed proposal, preview and publish reads reject symlinks and enforce bounded regular-file reads.
- Confirmed `origin/main` did not modify the four core implementation and contract test files since the original merge base; the rebase onto `b9a7263c50daaf57b8bda33d07c7be8b6dab80a7` was conflict-free.
- Corrected an earlier handoff inconsistency: commit `7bec4de197ad02988a1285061460957dffc8727b` actually had tree `a0dc62dc5c4c54118e689eb5d60844c522bc2620`, not the claimed `d134a5ddfb5b46e75ab555d48a4728ea8998a03c`.

## Validation

- Changed-file Ruff: PASS; receipt `8c11d0804ec1765291e5533a217f4f0f45471312553fef26db1eb26f590a7936`.
- Focused review, symlink, race and atomicity tests: 11 PASS; receipt `980d69be60ddad00de3d12ee45ed9e96b58194792ded0dfde86a9879be132852`.
- `tests/test_operator_intake.py`: 104 PASS; receipt `9d92417e65724e8d341708ef3ff0efcb2704885221c8cf963b7f478cabd57735`.
- `tests/test_lease_contract.py`: 14 PASS; receipt `062799f097d906c1ebca9260b2334b450ccd0f57b015d3f336db164d0627cb82`.
- Full `make validate`: Ruff PASS, 780 tests PASS, Systemkatalog boundary PASS, Bureau Registry valid, StateStore integrity `ok`; receipt `0d3c7029407f9fa6670326899a7a0fd3dfd7bab0efc8b83603b4a5b26b94db94`.
- `git diff --check origin/main...HEAD`: PASS.

## Non-claims

This review does not establish GitHub publication, Pull Request checks, merge, Bureau runtime deployment, live CLI behavior, the separate Grabowski typed adapter, typed end-to-end adoption, benchmark results or completion of `OPERATOR-MACHINE-READABILITY-V1-T017`.
