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
- optional `candidate_id` for `candidate_task` records
- optional `supersedes_event_id` for append-only candidate corrections or closeouts
- optional `note`
- `promotion_required`
- `does_not_establish`

Every newly recorded candidate receives a stable opaque `candidate_id`. To correct, close, drop or
promote an existing candidate, append a new candidate event that names the current event through
`--supersedes-event-id`. The successor inherits the candidate identity, repository, task binding,
status and `promotion_required` value unless the latter is explicitly changed with
`--promotion-required` or `--no-promotion-required`. Repository changes are rejected because they
would make per-repository projections ambiguous. A predecessor can be superseded only once.

Example correction and closeout:

```bash
bureau --root . --json live-register \
  --kind candidate_task \
  --title "Corrected candidate description" \
  --supersedes-event-id 31

bureau --root . --json live-register \
  --kind candidate_task \
  --title "Candidate closed after review" \
  --status closed \
  --supersedes-event-id 32
```

`live-list`, `what-now` and `repo-balls` preserve all sampled historical events in their raw record
list but derive open candidates from only the latest event per stable identity. The summary exposes
`candidate_history_count`, `superseded_candidate_event_count` and `latest_candidates`. Legacy
candidate events without an ID remain readable and receive a derived `candidate-event-<event-id>`
identity when first superseded. A malformed legacy predecessor without a required status fails
closed with an event-specific diagnostic. Promotion plans reject stale superseded events.

The output also includes derived summaries for active thread focus and active focus overrides.

## Boundaries

Live-register entries are operational evidence only. They do not establish:

- registry task truth;
- queue truth;
- claim authority;
- dispatch authority;
- merge readiness.

Promotion from candidate work to durable Bureau work must still go through a reviewed Registry PR.

## What-now and repo-balls integration

`bureau what-now` includes a `live_register` context block. This makes current thread focus,
focus overrides and candidate work visible next to registry/runtime ranking. The context is source
bound and does not change queue order, task eligibility, claimability or hard blockers.

`bureau repo-balls` includes a `live_register` overlay per repository and a
`live_register_summary`. This shows live focus per repo while `registry/queue.json` remains the only
dispatch queue.

## Conflict view

`bureau live-conflicts` is read-only. It compares live thread/worker focus with active runs and the
repo-balls/open-PR blocker surface:

```bash
bureau --root . --json live-conflicts --repo repo.bureau --capability repository
```

Findings can identify an active run overlapping a live focus, a worker bound to a different active
run, or an open-PR blocker visible for the repository. A finding is not a cleanup, claim or merge
authority.

## Candidate promotion

Candidate tasks can be turned into a reviewed Registry diff through a plan:

```bash
bureau --root . --json live-promote-plan   --event-id 12   --initiative BUREAU-LIVE-REGISTER-V1   --task-id BUREAU-LIVE-REGISTER-V1-T007   --write-plan /tmp/live-promote.json
```

The plan must be reviewed by setting `review.status=reviewed` and `reviewer`. Applying the plan
writes a task JSON file only:

```bash
bureau --root . --json live-promote-plan --apply-plan /tmp/live-promote.json
```

It does not mutate `registry/queue.json`, verify the task, claim work or dispatch an agent.

## Retention and Chronik export

`bureau live-retention` reports the current retention policy and sampled event counts. It has no
delete authority.

`bureau live-export --format chronik` emits a redacted Chronik-shaped summary with stable event IDs,
source timestamps and payload digests. It omits notes and does not import into Chronik by itself.

```bash
bureau --root . --json live-retention
bureau --root . --json live-export --format chronik --repo repo.bureau
```

## Implemented follow-up scope

This implementation completes the registered follow-ups for:

- `what-now` live-register context;
- `repo-balls` live-register overlay;
- reviewed candidate-to-registry task promotion plan;
- thread/worker conflict view;
- retention and redacted Chronik export boundary.
