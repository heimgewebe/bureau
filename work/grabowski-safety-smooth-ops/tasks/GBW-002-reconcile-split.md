# GBW-002 — Reconcile in Check, Refresh und Resume splitten

Status: READY
Priority: P0
Depends on: GBW-001

## Problem

`grabowski_task_reconcile` vermischt Diagnose, Statusaktualisierung, Lease-Freigabe und mögliches Resume. Lokal funktioniert der Pfad, aber die Plattform kann ihn wegen mutierender Annotation und High-Risk-Semantik blockieren.

## Ziel

Reconcile wird semantisch getrennt:

1. `grabowski_task_reconcile_check` — read-only, liefert `would_*`-Felder.
2. `grabowski_task_reconcile_refresh` — aktualisiert nur persistente Zustände und gibt terminale Leases frei, startet aber keine Prozesse.
3. `grabowski_task_reconcile_resume` — high-risk, explizit, retry-safe, bounded, auditiert.

## Akzeptanzkriterien

- `check` besitzt keine mutierende Annotation.
- `refresh` ruft kein systemd-run und kein Resume auf.
- `resume` verlangt task_id oder max_resumes plus reason.
- Tests decken running, completed, failed, outcome_unknown und retry-safe ab.
- Audit-Operationen sind getrennt: check optional none, refresh `task-reconcile-refresh`, resume `task-reconcile-resume`.

## Alternativpfad

Lokaler systemd-Reconcile-Timer schreibt Receipts; ChatGPT liest nur read-only Status. Das reduziert Plattformabhängigkeit stärker als interaktiver Reconcile.
