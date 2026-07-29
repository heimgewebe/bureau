# Bureau Lean GitHub Actions — T069

Stand: 29. Juli 2026
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

1. Der bereits entfernte Claude-Workflow bleibt abwesend. Ein Regressionstest verhindert, dass diese wirkungslose Workflowfläche unbemerkt zurückkehrt; die Entfernung selbst ist keine neue Einsparung dieses PR.
2. `validate` verwendet eine Workflow- und Event-gebundene Concurrency-Gruppe. Nur ältere Pull-Request-Läufe derselben PR werden supersediert; Push- und Merge-Group-Läufe besitzen getrennte Schlüssel und werden nicht storniert.
3. Der Pull-Request-Registry-Preflight verwendet eine eigene PR-gebundene Concurrency-Gruppe mit `cancel-in-progress: true`.
4. Die im ersten Entwurf vorgesehene Beschränkung neuer Main-Push-Freshness-Checks auf vorab erkannte Registry-Kandidaten wurde nach Review verworfen. Sie hätte bei unvollständiger Dateiermittlung einen älteren grünen PR-Check weitergelten lassen können.
5. Die bestehende Fail-closed-Grenze bleibt deshalb unverändert: Vor jeder per-PR-Dateiermittlung wird auf jedem bekannten offenen PR-Head ein neuer blockierender Freshness-Check veröffentlicht. Nichtkandidaten werden anschließend ohne Registry-Allokationsprüfung erfolgreich abgeschlossen. Gezielte Regressionstests binden diese Reihenfolge und den Fehlerpfad.

## Gemessener Effekt

| Messachse | Ausgang | Ergebnis / gebundene Aussage |
| --- | ---: | --- |
| übersprungene Claude-Läufe im registrierten Fenster | 18 | historische Leerlaufläufe; die bereits bestehende Abwesenheit wird nun regressionsgeschützt, ohne neue Einsparung zu beanspruchen |
| supersedierbare Validate-/Preflight-Läufe beim Live-Readback | 0 | 0 unmittelbar stornierbar; künftige Einsparungen werden nicht hochgerechnet |
| offene PRs beim Live-Readback am 29. Juli 2026, 17:12 UTC | 2 | weiterhin 2 leichte Freshness-Check-Publikationen pro Main-Push als notwendige Fail-closed-Grenze |
| Added/Renamed-Registry-Task-Kandidaten im Livebestand | 0 | sowohl vorher als auch nachher 0 eigentliche Registry-Allokationsprüfungen |
| Required-Check-Namen und Python-Matrix | 3 Checks; 3.10/3.12 | unverändert |

Die beiden offenen PRs waren #1176 und #1178. Ihre Registry-Taskdateien waren jeweils **modifiziert**, nicht hinzugefügt oder umbenannt; #1178 änderte zusätzlich `registry/queue.json`. Beide waren daher keine Registrierungsallokationskandidaten. Dieser Bestand belegt keine konkrete Laufzeitersparnis durch die neue Concurrency, sondern nur deren korrekte Begrenzung.

## Sicherheitsgrenzen

Die Änderung verleiht keine Review-, Merge-, Queue-, Claim-, Dispatch-, Deployment- oder Cleanup-Autorität. Branchschutz, Merge Queue, Codex-Cloud-Einstellungen und Checknamen bleiben unverändert. Die Main-Push-Invalidierung aller offenen PR-Heads bleibt bewusst erhalten. Konflikte oder Infrastrukturfehler werden weiterhin als fehlgeschlagener beziehungsweise blockierender `registry-registration-preflight/freshness`-Check auf dem exakten PR-Head sichtbar.
