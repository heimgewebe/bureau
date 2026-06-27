# Bureau

Bureau is the deterministic coordination and dispatch layer between plans and real work.

Its core interaction is:

```text
Look into Bureau and execute the next task.
```

Each invocation reconciles earlier runs, computes the executable frontier, atomically claims one
compatible task, freezes an execution envelope and reserves its coordination scopes. A subsequent
invocation therefore receives a different non-conflicting task or an explicit explanation that no
further safe parallel work is available.

## Boundaries

- **Bureau** owns commitments, ordering, dependencies, coordination claims, dispatch and completion.
- **Grabowski** owns processes, hosts, concrete runtime leases, durable tasks and workers.
- **Steuerboard** owns action-specific readiness and specialised evidence.
- **Cabinet** owns readable research and decisions.
- **Schauwerk** owns visual projections.
- **Chronik** owns append-only events.

Bureau does not implement another shell, task runner, runtime lease engine, knowledge base or
project-management UI.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
make validate

bureau --root . frontier --capability repository --capability shell
bureau --root . claim-next --worker chatgpt-session-1 \
  --capability repository --capability shell
```

Operational state is outside Git at `~/.local/state/bureau/bureau.sqlite3`. Override it with
`BUREAU_STATE_DIR` or `--state-db`.

## Implemented in v0.1

- JSON registry contracts and semantic validation;
- dependency-cycle detection;
- hierarchical read/write/exclusive/capacity claims;
- deterministic frontier computation;
- SQLite WAL/FULL synchronous dispatch state;
- atomic `claim-next` and one active run per task/worker;
- immutable execution envelopes;
- dynamic claim expansion;
- worker heartbeats and orphan reconciliation;
- evidence-complete receipts;
- isolated Git worktree creation;
- Grabowski handoffs with `origin_ref` and `request_id`;
- concurrent claim stress tests.
