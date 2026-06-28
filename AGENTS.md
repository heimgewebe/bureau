# Bureau agent contract

1. Run reconciliation before claiming or checking out new work.
2. Use one stable worker/session identity and claim exactly one task per interactive worker.
3. Never use resources outside the run reservations; expand the claim before expanding scope.
4. Mutating Git work uses the recorded run-specific worktree unless explicitly exempted.
5. Never overwrite, clean or remove an unknown dirty worktree.
6. Keep task and plan revisions frozen for the duration of a run.
7. Bind the external executor before reporting that execution started.
8. Treat an unavailable external adapter as an explicit blocked observation, never as success.
9. Process exit is not completion; every acceptance criterion needs typed evidence.
10. Never edit an active execution envelope.
11. Merge, rebase and deployment are separate exclusive tasks.
12. Remove a workspace only after a terminal run and a clean, merged status unless force is explicit.
