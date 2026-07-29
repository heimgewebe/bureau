# Bureau Lean GitHub Actions — T069

Stand: 29. Juli 2026, 18:35 UTC  
Gebundene Basis: `6f3b7c59f30b94d7262a1a418fb0ef444c026048`

## Revisionsgebundene Ausgangslage

| Fläche | Ausgang auf `6f3b7c59f30` |
| --- | --- |
| `.github/workflows/claude.yml` | nicht vorhanden; Entfernung bereits durch Commit `94b79ef` belegt |
| `.github/workflows/validate.yml` | Basis-SHA-256 `96ce6d1cedda48a7ed3636fc8d6e7e387b706ed0bac64ab3a87e62690aa48dc3`; Ergebnis-SHA-256 `2e53a6682ee8427407bb93d47520897dfdf6ee6b6acea5a2a7c8fae4e5fe2ae9`; Checkmatrix bleibt 3.10/3.12 |
| `.github/workflows/registry-registration-preflight.yml` | Basis-SHA-256 `17959a6bff19f6ed2053a28da794543d5384d05a24209d4c190643f0707db559`; Ergebnis-SHA-256 `8d954c04c66cdd114f2459a0a111833bff57402334ba9b3c5cf06ff8f3a11850` |
| Required Checks | `validate (3.10)`, `validate (3.12)`, `registry-registration-preflight/freshness` unverändert |

Der registrierte Messzeitraum vom 28. Juli 2026, 06:35:31–12:45:59 UTC umfasste 100 Actions-Läufe, 18 vollständig übersprungene Claude-Läufe, 24 Validate-Läufe, 25 Registry-Preflight-Läufe, sieben offene PRs und 90 Registry-only-PRs im betrachteten Bestand. Diese Zahlen sind Beobachtungen, keine Kostenprognose.

## Änderung

1. Der bereits entfernte Claude-Workflow bleibt abwesend; normale Issues, Reviews und Reviewkommentare erzeugen dadurch keine Claude-Actions-Läufe.
2. `validate` verwendet eine Workflow- und Event-gebundene Concurrency-Gruppe. Nur Pull-Request-Läufe derselben PR werden supersediert; Push- und Merge-Group-Läufe besitzen getrennte Schlüssel und werden nicht storniert.
3. Der Pull-Request-Registry-Preflight verwendet eine eigene PR-gebundene Concurrency-Gruppe mit `cancel-in-progress: true`.
4. Die Main-Push-Revalidierung veröffentlicht vor jeder Dateiermittlung einen neuen, blockierenden `registry-registration-preflight/freshness`-Check auf jedem bekannten offenen PR-Head. Erst danach werden die paginierten Dateilisten geprüft. Nur PRs mit hinzugefügten oder umbenannten `registry/tasks/*.json`-Dateien führen die eigentliche Registry-Allokationsprüfung aus; Nichtkandidaten werden nach der sicheren Invalidierung unmittelbar wieder erfolgreich abgeschlossen.
5. Fehler bei Pagination, Dateiabfrage oder Kandidatenmetadaten können dadurch keinen älteren grünen Check weiterverwenden: Der betroffene PR bleibt mit einem fehlgeschlagenen oder nicht abgeschlossenen neuen Check gesperrt.

## Gemessener Effekt

| Messachse | Vorher | Nachher / gebundene Aussage |
| --- | ---: | --- |
| übersprungene Claude-Läufe im registrierten Fenster | 18 | 0 aus diesem entfernten Workflow; 18 beobachtete Leerlaufläufe werden für dasselbe Fenster vermieden |
| offene PRs bei Live-Readback am 29. Juli 2026, 17:12 UTC | 2 | 2 vollständig paginiert und sicher invalidiert |
| davon Added/Renamed-Registry-Task-Kandidaten | 0 | 0 eigentliche Registry-Allokationsprüfungen beim nächsten Main-Push |
| pauschale eigentliche Registry-Revalidierungen bei diesem Livebestand | 2 | 0; genau 2 unnötige Registry-Prüfungen pro Main-Push des gebundenen Bestands entfallen |
| leichte Freshness-Check-Publikationen | 2 | 2 bleiben als notwendige Fail-closed-Grenze erhalten |
| aktuell parallel laufende supersedierbare Validate-/Preflight-Läufe | 0 | 0 unmittelbar stornierbar; künftige Einsparungen werden nicht hochgerechnet |

Die beiden offenen PRs waren #1176 und #1178. Ihre Registry-Taskdateien waren jeweils **modifiziert**, nicht hinzugefügt oder umbenannt; #1178 änderte zusätzlich `registry/queue.json`. Beide sind daher keine Registrierungsallokationskandidaten.

## Sicherheitsgrenzen

Die Änderung verleiht keine Review-, Merge-, Queue-, Claim-, Dispatch-, Deployment- oder Cleanup-Autorität. Branchschutz, Merge Queue, Codex-Cloud-Einstellungen und Checknamen bleiben unverändert. Die Laufzeitoptimierung gilt nur für die eigentliche Registry-Allokationsprüfung; die vorgelagerte Check-Invalidierung aller offenen PR-Heads bleibt bewusst erhalten. Konflikte oder Infrastrukturfehler werden als fehlgeschlagener beziehungsweise blockierender `registry-registration-preflight/freshness`-Check auf dem exakten PR-Head sichtbar.
