# Bureau agent contract

1. Reconcile before claiming new work.
2. Claim exactly one task per interactive worker.
3. Do not use resources outside the run reservations.
4. Extend the claim before expanding scope.
5. Mutating Git work uses a run-specific worktree unless explicitly exempted.
6. Never overwrite or remove an unknown dirty worktree.
7. Bind the external executor before reporting that execution started.
8. Process exit is not completion; every acceptance criterion needs evidence.
9. Never edit an active execution envelope.
10. Merge, rebase and deployment are separate exclusive tasks.
