# GBW-001 — Operating Baseline und Receipts einfrieren

Status: IN_PROGRESS
Priority: P0
Started: 2026-06-29T16:04:00+02:00

## Ziel

Vor weiteren Grabowski-Änderungen wird der aktuelle Betriebszustand dokumentiert: Runtime, Contract, Audit, Taskzustand, Reconcile-Verhalten und Plattformblockaden.

## Akzeptanzkriterien

- `receipts/gbw-001-started.md` existiert.
- `receipts/platform-block-matrix.md` existiert.
- Runtime- und Contractstatus sind referenziert.
- Nächster PR ist GBW-002: Reconcile-Split.

## Risiko

Ohne Baseline werden spätere Fixes schwer beweisbar. Mit Baseline entsteht kaum Risiko, weil der Schritt read-only bzw. dokumentierend ist.

## Nächste konkrete Aktion

GBW-002 als Code-Branch im Grabowski-Repo vorbereiten: `task_reconcile_check`, `task_reconcile_refresh`, `task_reconcile_resume`.
