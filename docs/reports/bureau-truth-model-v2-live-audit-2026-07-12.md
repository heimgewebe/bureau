# Bureau Truth Model v2 live audit — 2026-07-12

## Scope

This audit re-read Grabowski runtime, the Bureau checkout, GitHub PR state, Bureau diagnostics, the state store, queue, repository frontier and Live Register behavior. Foreign dirty states, worktrees and processes were not modified. The idle Systemaudit closeout lease was taken over only after its worktree was proven clean, no task or process remained, the exact PR diff hash matched, CI was green and live Doctor was healthy; PR #446 was then merged with expected-head protection and its owner-bound leases were released normally.

## Live baseline

- Grabowski connector and deployment contract were healthy and release-bound.
- Bureau `origin/main` advanced repeatedly during the audit: PR #443 produced `2478fd69e66db3bd29fa7957092e914d00a580d4`, PR #444 advanced to `5c229e8782e0d5840c0d7f5f8fd6d06663a0ed5c`, PR #445 to `7309d6378d28fa2b95c5789cc75e2f9cebde2468`, and the verified Systemaudit closeout PR #446 to merge commit `87540710b741399ed21e8186d0a0f96c636dad81`.
- The main checkout remained seven commits behind and contained two foreign registry modifications. It was not reset, updated or reused; all Truth Model work used a separately leased worktree based on `origin/main`.
- PR #443 removed the first observed lifecycle mismatch; PR #446 later removed the remaining RPU completion mismatch. Live Doctor and Registry Truth were healthy after PR #446.
- The queue contained one `now` task, three `next` tasks and 56 `later` tasks. `BUR-2026-005-T015` remained planned in `later`.
- The state store passed SQLite quick check and foreign-key check. It contained no active runs or reservations at the sampled time.

## Confirmed P0 defect: bounded history corrupted current projection

The current implementation derived active thread focus, focus overrides, candidates and conflict context from only the most recent 50 or 100 Live Register events.

A deterministic reproduction appended one active focus and then 120 unrelated events. The older focus disappeared from `live-list`, `live_register_context`, repository context and conflict reporting.

The real state store showed the same defect:

- 176 Live Register events at the first measurement, later 177;
- active Grabowski focus at event 36;
- default 50- and 100-event projections omitted event 36;
- a 500-event read restored it.

This was an operational correctness defect, not a display-only issue.

## Implemented correctness-first repair

The raw history list remains bounded by the requested display limit. Current-state projection scans the complete Live Register event basis and exposes:

- `coverage_complete`;
- `history_truncated`;
- `oldest_loaded_event_id`;
- `projection_source`.

Conflict reporting emits a blocker when projection coverage is incomplete. The initial source is explicitly `complete_event_scan`; an indexed or materialized replacement is a separate measured follow-up.

The first live read with the repaired code loaded 50 displayed events from a 177-event history, marked history as truncated, and still projected active event 36. `repo-balls` then showed five Live Register repositories instead of four, and `what-now` exposed 42 open candidates instead of the previously truncated 14. A later pre-publication read covered 191 events, still retained event 36 and reported 44 open candidates.

## Confirmed T015 acceptance gap

PR #344 merged the reviewed queue-reconcile apply path and implemented queue hash binding, dry-run parity, expected queue recomputation, post-apply gates, rollback and no claim/dispatch/completion/merge authority.

However, the plan recorded `registry.git_head` while apply never compared it with the current registry head. Therefore acceptance criterion `reviewed-plan-required` was not fully satisfied. T015 must not be closed merely because PR #344 merged. A dedicated follow-up enforces non-null head binding and refusal after head drift before T015 verification.

## Additional truth-model findings registered for later work

- A narrow read-only status capsule is absent; full status access still depends on the broader operator path.
- Registry/implementation drift is not detected acceptance-by-acceptance.
- Lifecycle-ledger reduction lacks shadow-mode parity evidence.
- Repository frontier exists but was incomplete while Live Register projection was truncated.
- Long-lived backlog tasks lack normalized revalidation metadata.
- One historical Schauwerk workspace row remained active while its run was orphaned.
- Before PR #443, Doctor reported lifecycle blockers while status-projection reported healthy; parity semantics need an explicit contract even though the immediate mismatch was fixed.
- The complete projection exposed 42 open candidates initially and 44 on the later pre-publication read; old observations need source-aware revalidation rather than automatic deletion.

## Authority boundaries

The repair and registered roadmap do not add automatic queue mutation, task verification, claim, dispatch, merge, deployment or cleanup authority. Git remains durable task truth; Live Register remains operational context.
