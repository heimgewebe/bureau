# Open PR task metadata contract v1

Bureau treats open pull requests as external reservations. A PR may bind itself
to one or more Bureau tasks with structured task metadata. Structured metadata is
preferred over broad title, body or branch-name heuristics.

## Supported structured markers

Use one of these forms:

- PR body line: `Bureau-Task: BUR-2026-005-T010`
- PR body line with multiple tasks: `Bureau-Tasks: BUR-2026-005-T010, BUR-2026-005-T011`
- PR label: `Bureau-Task: BUR-2026-005-T010`
- PR label: `Bureau-Task/BUR-2026-005-T010`
- structured API metadata keys when provided by tests or future adapters:
  `bureau_task`, `bureau_tasks`, `bureauTask`, `bureauTasks`, `task_id`, `task_ids`,
  `taskId`, or `taskIds`.

Task IDs are matched against known Registry task IDs. Matching is
case-insensitive for ASCII letters and treats underscores as hyphens, but it
still uses task-ID token boundaries so `T001` does not match `T0010` or
`T001-EXTRA`.

## Fallback matching

If no structured marker is present, Bureau may fall back to existing lower
confidence heuristics:

- exact task-ID text in PR title or body;
- branch suffixes such as `feat/bur-2026-005-t010-metadata-contract`.

Fallback matches are compatibility aids only. They do not prove task completion,
review readiness, merge readiness, CI sufficiency, runtime correctness or claim
truth.

## Boundary

Structured metadata improves duplicate detection and open-PR claim blocking. It
does not create tasks, verify tasks, dispatch work, approve reviews, merge PRs or
mutate runtime state.
