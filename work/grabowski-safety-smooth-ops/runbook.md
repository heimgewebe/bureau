# Runbook: Grabowski Safety & Smooth Ops

## Normalmodus

- Diagnose bevorzugt über dedizierte read-only Tools.
- Kein generischer Terminalpfad, wenn ein spezifisches Tool existiert.
- Optionalfelder in Toolaufrufen weglassen statt explizit null senden, sofern möglich.
- Plattformblocks werden als Diagnoseereignis dokumentiert, nicht mit Rechteausweitung beantwortet.

## Reconcile-Zielmodell

- `task_reconcile_check`: read-only, keine DB-Schreiboperation, keine Lease-Freigabe, keine Resume-Aktion.
- `task_reconcile_refresh`: begrenzte State-Aktualisierung, terminale Leases freigeben, keine Prozesse starten.
- `task_reconcile_resume`: high-risk, nur explizit, retry-safe, begründet, auditiert und begrenzt.

## Break-Glass

Nur mit Zweck, Ablaufzeit, Auditmarker und Rückkehr in Normalmodus:

- Terminal Run
- Secret Reveal
- Destroy Path
- Browser Profile Read
- Process Signal

## Abnahmekriterien für GBW-001

1. Runtime- und Contractstatus dokumentiert.
2. Task-/Reconcile-Zustand dokumentiert.
3. Plattformblockaden und Drive-Transfer-Ausweichpfad dokumentiert.
4. Nächster Code-PR eindeutig: GBW-002.
