# Grabowski grip roadmap rest v1

Source: user-provided Komplettplan "Grabowski handlungsfähiger, flexibler, dauerfähiger".

Already implemented upstream in `heimgewebe/grabowski` before this registration:

- operator grip foundation (`repo-orient`, `pr-check-readiness`, `post-merge-sync`)
- branch and PR publishing grips (`branch-publish`, `pr-create-or-update`)
- review/readiness hardening for structured external review evidence

Remaining registration scope:

1. worktree navigation grips (`worktree-orient`, checkout classification, cleanup planning)
2. privileged grip receipts (`runtime-deploy-check`, `runtime-deploy`, `service-restart`, later `pr-merge`)
3. durable scout
4. mechanic durable normal actions
5. captain privileged actions
6. autonomy/doktrin docs update

Non-goals for this registration:

- no immediate deploy or merge authority change
- no new event bus or dashboard
- no Bureau hard-gate for every Grabowski grip


## Roadmap slice mapping

| Source roadmap slice | Bureau task | Lane | Notes |
| --- | --- | --- | --- |
| PR4/read-only subset | GRIP-ROADMAP-V1-T001 | now | Worktree navigation first; implements a read-only grip but requires a write claim while code is changed. |
| PR3 | GRIP-ROADMAP-V1-T002 | next | Privileged receipt foundation after worktree orientation; no Captain default enablement. |
| PR5 | GRIP-ROADMAP-V1-T003 | next | Durable Scout prototype remains non-mutating. |
| PR6 | GRIP-ROADMAP-V1-T004 | later | Mechanic normal-action loop depends on earlier grip and scout foundations. |
| PR7 | GRIP-ROADMAP-V1-T005 | later | Captain path remains explicitly high-impact and receipt-bound. |
| PR8 | GRIP-ROADMAP-V1-T006 | later | Doctrine docs after mechanics and high-impact language are precise. |

Queue order remains `now`, `next`, `later`. Task claim mode describes what the implementation task needs to change in the repository, not the effect of the resulting Grabowski grip.
