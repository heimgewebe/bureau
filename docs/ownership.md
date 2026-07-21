# Ownership

Bureau separates coordination ownership from operational observation. The core question is: who is allowed to make a fact true, and who is only reporting that fact to Bureau?

| Concern | Owner |
|---|---|
| commitments, queue order, dependencies and coordination conflicts | Bureau Core |
| execution envelopes, receipts, task overlays and lifecycle verification | Bureau Core |
| closure, review stewardship, source bridges, Cabinet bridges, agent frontier and Codex bridge observations | Bureau Ops |
| host/process execution, concrete leases, durable tasks and workers, plus live Git/network/branch/worktree effects | Grabowski |
| read-only repository observation and source-bound readiness/evidence derivation; no approval or execution authority | Steuerboard |
| readable research and decisions | Cabinet |
| visual projection | Schauwerk |
| append-only events | Chronik |
| branches, pull requests, reviews, checks and CI conclusions | GitHub |

## Authority rules

Bureau Core may record evidence from operational organs, but the evidence remains tied to its source authority. A GitHub check conclusion is still a GitHub fact after Bureau records it. A Grabowski process state is still a Grabowski fact after Bureau binds it to a run. A Cabinet synthesis is still a Cabinet decision or research note after Bureau references it.

Bureau Ops may observe GitHub, Grabowski, Steuerboard, Cabinet, Schauwerk, Chronik or repository facts and turn them into Bureau-shaped findings, candidate tasks, verification records or receipts. It does not replace the owner of the source fact.

Bureau Ops must not turn observations into commitments without an explicit Bureau Core change. It must not mark tasks complete without revision-bound evidence, replace another authority as owner of its facts, or hide stale, partial or unverifiable observations behind a green lifecycle state.

## Practical examples

- A PR number, mergeability and CI result are GitHub-owned. Bureau may cite them in `metadata.verification`, but GitHub remains the authority for whether the PR existed and what its checks concluded.
- A durable task PID, lease or worker status is Grabowski-owned. Bureau may bind that external identity to a run, but cannot infer process success as Bureau completion without acceptance evidence.
- A Cabinet decision can justify why a task exists. It does not by itself make a Bureau task ready, verified or completed.
