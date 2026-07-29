# Bureau Lean GitHub Actions — T069

Stand: 29. Juli 2026, 18:12 UTC
Gebundene Basis: `9cf78d4e2788447bf68e5538d71c218ca08f74a5`

## Revisionsgebundene Ausgangslage

| Fläche | Ausgang auf `9cf78d4e2788` |
| --- | --- |
| `.github/workflows/claude.yml` | nicht vorhanden; Entfernung bereits durch Commit `94b79ef` belegt |
| `.github/workflows/validate.yml` | Basis-SHA-256 `96ce6d1cedda48a7ed3636fc8d6e7e387b706ed0bac64ab3a87e62690aa48dc3`; Ergebnis-SHA-256 `2e53a6682ee8427407bb93d47520897dfdf6ee6b6acea5a2a7c8fae4e5fe2ae9`; Checkmatrix bleibt 3.10/3.12 |
| `.github/workflows/registry-registration-preflight.yml` | Basis-SHA-256 `17959a6bff19f6ed2053a28da794543d5384d05a24209d4c190643f0707db559`; Ergebnis-SHA-256 `88f9e7a378b826a609f47235f9d6083515a1ff73aba255a2a68c79ecab563fac` |
| Required Checks | `validate (3.10)`, `validate (3.12)`, `registry-registration-preflight/freshness` unverändert |

Der registrierte Messzeitraum vom 28. Juli 2026, 06:35:31–12:45:59 UTC umfasste 100 Actions-Läufe, 18 vollständig übersprungene Claude-Läufe, 24 Validate-Läufe, 25 Registry-Preflight-Läufe, sieben offene PRs und 90 Registry-only-PRs im betrachteten Bestand. Diese Zahlen sind Beobachtungen, keine Kostenprognose.

## Änderung

1. Der bereits entfernte Claude-Workflow bleibt abwesend; normale Issues, Reviews und Reviewkommentare erzeugen dadurch keine Claude-Actions-Läufe.
2. `validate` verwendet eine Workflow- und Event-gebundene Concurrency-Gruppe. Nur Pull-Request-Läufe derselben PR werden supersediert; Push- und Merge-Group-Läufe besitzen getrennte Schlüssel und werden nicht storniert.
3. Der Pull-Request-Registry-Preflight verwendet eine eigene PR-gebundene Concurrency-Gruppe mit `cancel-in-progress: true`.
4. Die Main-Push-Revalidierung ermittelt zunächst vollständig alle offenen PRs und deren paginierte Dateilisten. Im Erfolgsfall werden CheckRuns ausschließlich für PRs mit hinzugefügten oder umbenannten `registry/tasks/*.json`-Dateien erzeugt. Schlägt die Dateiabfrage eines eindeutig identifizierten PR fehl oder fehlen bei einem erkannten Registry-Kandidaten lesbare Head-Metadaten, wird vor dem Abbruch ein fehlgeschlagener Freshness-Check auf dessen exaktem Head veröffentlicht. Bei jeder unvollständigen Kandidatenermittlung erhalten außerdem alle bereits eindeutig erkannten Registry-Kandidaten einen fehlgeschlagenen Freshness-Check, bevor der Lauf endet. Eine nicht vollständig lesbare PR-Gesamtliste bleibt ein fehlschlagender Main-Lauf, weil ohne belastbare PR-Identität kein korrekter Head-Check publiziert werden kann.

## Fail-closed-Bindung

Die Kandidatenbeschränkung ist an den am 29. Juli 2026 live gelesenen Branchschutz von `main` gebunden: `required_status_checks.strict=true`; erforderlich bleiben `validate (3.10)`, `validate (3.12)` und `registry-registration-preflight/freshness`. Nach jedem neuen Main-Commit ist ein nicht aktualisierter Pull Request damit unabhängig von einem älteren grünen Check nicht mergefähig. Sobald sein Head aktualisiert wird, erzeugt `pull_request_target` den erforderlichen Freshness-Check auf dem neuen Head.

Dieser Pull Request verändert den Branchschutz nicht. Falls `strict` künftig deaktiviert wird, ist die kandidatenbeschränkte Main-Push-Revalidierung nicht mehr als fail-closed belegt und darf ohne einen gleichwertigen Ersatz nicht beibehalten werden.

## Gemessener Effekt

| Messachse | Vorher | Nachher / gebundene Aussage |
| --- | ---: | --- |
| übersprungene Claude-Läufe im registrierten Fenster | 18 | 0 aus diesem entfernten Workflow; 18 beobachtete Leerlaufläufe werden für dasselbe Fenster vermieden |
| offene PRs bei Live-Readback am 29. Juli 2026, 18:05 UTC | 4 | 4 vollständig paginiert |
| davon Added/Renamed-Registry-Task-Kandidaten | 0 | 0 neue Freshness-CheckRuns beim nächsten Main-Push |
| pauschal revalidierte PRs bei diesem Livebestand | 4 | 0; genau 4 unnötige Revalidierungen pro Main-Push des gebundenen Bestands entfallen |
| aktuell parallel laufende supersedierbare Validate-/Preflight-Läufe | 0 | 0 unmittelbar stornierbar; künftige Einsparungen werden nicht hochgerechnet |

Die vier offenen PRs waren #1176, #1179, #1180 und #1182. #1176, #1180 und #1182 modifizierten vorhandene Registry-Taskdateien; #1180 modifizierte zusätzlich eine Initiative. #1179 änderte ausschließlich Workflows, Tests und diesen Bericht. Keiner fügte eine `registry/tasks/*.json`-Datei hinzu oder benannte eine solche um; damit war keiner ein Registrierungsallokationskandidat.

## Sicherheitsgrenzen

Die Änderung verleiht keine Review-, Merge-, Queue-, Claim-, Dispatch-, Deployment- oder Cleanup-Autorität. Branchschutz, Merge Queue, Codex-Cloud-Einstellungen und Checknamen bleiben unverändert. Unvollständige Kandidatenermittlung schlägt fehl; für jeden dabei eindeutig identifizierbaren PR wird zuvor ein blockierender Freshness-Check veröffentlicht, statt einen früheren grünen PR-Head-Check weitergelten zu lassen. Konflikte einer prüfbaren Registry-Allokation werden weiterhin als fehlgeschlagener `registry-registration-preflight/freshness`-Check auf dem exakten PR-Head veröffentlicht.
