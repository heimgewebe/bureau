# OPERATOR-INTEGRATION-LOOP-V1-T005: koordinierter Claim-/Lease-Vertrag

## Ziel

Bureau bleibt die einzige Wahrheit für Taskauswahl, Run, Reservierungen und Abschluss. Grabowski bleibt die Wahrheit für live gehaltene Ressourcen-Leases. Der Übergang wird nicht als verteilte ACID-Transaktion ausgegeben, sondern als zweiphasiger, revisionsgebundener Vertrag mit fail-closed Readback und Kompensation umgesetzt.

## Phasen

1. `claim-intent` liest Queue, Task-/Planrevision, offene PRs, aktive Runs, Ressourcen und Runtimezustand. Es mutiert weder Bureau noch Grabowski.
2. Der Intent enthält einen vorab erzeugten Run-Identifier, die exakten Grabowski-Ressourcenschlüssel, eine gebundene Operatorfreigabe und die geplante Worktree-/Branch-Identität.
3. Grabowski erwirbt alle Schlüssel unter `bureau-run:<run_id>` und bindet jede Lease-Metadatenmenge an `task_id`, `run_id` und `claim_intent_sha256`.
4. `claim-commit` prüft Intent-Digest, Ablaufzeit, Task-/Planrevision, Runtimezustand, aktuelle Eligibility, offene PRs sowie den seit dem Intent unveränderten Repository-Head.
5. Innerhalb derselben Bureau-SQLite-Transaktion wird die private Grabowski-Lease-Datenbank live gelesen; erst danach werden Worker, Run und Reservierungen gemeinsam geschrieben. Die Execution Envelope enthält Intent, normalisierten Lease-Readback und Operatorfreigabe.
6. Bei Fehlern der Envelope-Materialisierung oder Workspace-Erstellung wird ein bereits angelegter Run terminalisiert und seine Bureau-Reservierung freigegeben. Der Rückgabevertrag weist die extern freizugebenden Grabowski-Leases exakt aus.
7. `claim-coordination-status` klassifiziert aktive Runs mit gültiger Lease als `active-bound`, Drift als blockierend, terminale Runs mit noch lebender Lease als `terminal-release-pending` und fehlende beziehungsweise abgelaufene terminale Leases als `terminal-released-or-expired`.

## Sicherheitsgrenzen

- Kein automatisches Lösen fremder Leases.
- Keine Workspace-Löschung aus Task- oder Lease-Abwesenheit.
- Keine zweite PR-, Task- oder Abschlusswahrheit.
- Keine Behauptung verteilter Datenbankatomizität.
- Kein Dispatch, Merge oder Deployment aus einem Intent.
- Der alte `claim-next`-Pfad bleibt kompatibel, ist aber nicht der koordinierte Adapterpfad.

## CLI

- `claim-intent`: read-only Planung; Review-before-effect wird nur mit explizitem `--approve` und run-/taskgebundener Evidenz zugelassen.
- `claim-commit`: effectful Commit eines exakten Intent- und Lease-Bindings; optional mit `--workspace`.
- `claim-coordination-status`: read-only Recovery- und Releaseprojektion.

## Recovery

Ein Intent ohne Run ist wirkungslos und darf nach Ablauf verworfen werden. Ein Run ohne Workspace wird bei Workspacefehler terminalisiert. Ein aktiver Run ohne gültige Lease ist blockierend und darf nicht still fortgesetzt werden. Ein terminaler Run mit lebender Lease erzeugt eine konkrete Releasepflicht; erst ein autoritativer Bureau-Terminalreadback erlaubt Grabowski, genau die gebundenen Schlüssel zu lösen.
