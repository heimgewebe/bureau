# Bureau Live Register v1

Status: first slice implemented in this PR.

## Decision

Bureau keeps two different truth layers:

| Layer | Storage | Purpose |
|---|---|---|
| Registry truth | Git JSON and reviewed PRs | Durable commitments, task definitions, acceptance, policy and verified outcomes. |
| Live register | Bureau state-store events | Gitless operational focus, thread balls, focus overrides and candidate work. |

The Live Register does not replace `registry/queue.json`. It records current operator attention and
candidate work without requiring a Queue PR for every focus movement.

## Commands

Record a thread focus:

```bash
bureau --root . --json live-register \
  --kind thread_focus \
  --thread-id chat-20260710-a \
  --repo repo.bureau \
  --title "Design live register" \
  --source chat
```

Record a candidate task that still needs durable promotion:

```bash
bureau --root . --json live-register \
  --kind candidate_task \
  --repo repo.bureau \
  --title "Integrate live register with what-now" \
  --promotion-required
```

List current live-register evidence:

```bash
bureau --root . --json live-list
bureau --root . --json live-list --kind thread_focus
bureau --root . --json live-list --repo repo.bureau
bureau --root . --json live-list --thread-id chat-20260710-a
```

## Event shape

Live-register events are stored in the existing state-store `events` table with
`event_type=live-register`. Payloads contain:

- `schema_version`
- `kind`: `thread_focus`, `candidate_task` or `focus_override`
- `title`
- `source`
- `status`
- optional `thread_id`
- optional `repo`
- optional `task_id`
- optional `note`
- `promotion_required`
- `does_not_establish`

The output includes a derived summary for active thread focus, active focus overrides and candidate
work requiring promotion.

## Boundaries

Live-register entries are operational evidence only. They do not establish:

- registry task truth;
- queue truth;
- claim authority;
- dispatch authority;
- merge readiness.

Promotion from candidate work to durable Bureau work must still go through a reviewed Registry PR.

## Follow-up boundaries

This first slice deliberately does not integrate Live Register into `what-now`, `repo-balls`,
Candidate-to-Registry promotion, Chronik export or retention policy. Those follow-up topics are
registered under `BUREAU-LIVE-REGISTER-V1`.
