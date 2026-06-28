# Operations

## Diagnose

```bash
bureau --root . doctor --json
bureau --root . lifecycle --json
bureau --root . explain-next --capability repository --capability shell --json
```

## Check out work

```bash
bureau --root . checkout-next --worker <stable-session-id> \
  --capability repository --capability shell --json
```

Use a stable session ID. Repeating the command returns the existing active assignment rather than
claiming another task.

## Complete

Evidence is a JSON object keyed by acceptance criterion ID:

```bash
bureau --root . complete <run-id> --evidence evidence.json --json
```

Completion is idempotent. SQLite is canonical; the receipt file is a deterministic materialisation.

## Workspaces

```bash
bureau --root . workspace-status <run-id> --json
bureau --root . workspace-preserve <run-id> --reason 'needs review'
bureau --root . workspace-cleanup <run-id>
```

Cleanup requires a terminal run. Dirty or unmerged workspaces are preserved unless `--force` is
explicitly supplied.
