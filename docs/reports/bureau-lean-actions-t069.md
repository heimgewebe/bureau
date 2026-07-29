# Bureau Lean GitHub Actions — T069

Stand: 29. Juli 2026, 19:23 UTC
Gebundene Basis: `f46ff846fa9f0e37be22c7f2e7ef4ce701fb1adc`

## Revisionsgebundene Ausgangslage

| Fläche | Ausgang auf `f46ff846fa9f` | Ergebnis |
| --- | --- | --- |
| `.github/workflows/claude.yml` | nicht vorhanden; Entfernung bereits durch Commit `94b79ef` belegt | unverändert abwesend |
| `.github/workflows/validate.yml` | SHA-256 `2e53a6682ee8427407bb93d47520897dfdf6ee6b6acea5a2a7c8fae4e5fe2ae9`; PR-gebundene Concurrency seit PR #1179 vorhanden | unverändert |
| `.github/workflows/registry-registration-preflight.yml` | SHA-256 `8d954c04c66cdd114f2459a0a111833bff57402334ba9b3c5cf06ff8f3a11850` | SHA-256 `88f9e7a378b826a609f47235f9d6083515a1ff73aba255a2a68c79ecab563fac` |
| Required Checks | `validate (3.10)`, `validate (3.12)`, `registry-registration-preflight/freshness` | unverändert |

Der registrierte Messzeitraum vom 28. Juli 2026, 06:35:31–12:45:59 UTC umfasste 100 Actions-Läufe, 18 vollständig übersprungene Claude-Läufe, 24 Validate-Läufe, 25 Registry-Preflight-Läufe, sieben offene PRs und 90 Registry-only-PRs im betrachteten Bestand. Diese Werte bleiben historische Beobachtungen und sind keine Kostenprognose.

## Änderung

1. Der bereits entfernte Claude-Workflow bleibt abwesend. Dieser Follow-up beansprucht daraus keine neue Einsparung.
2. Die durch PR #1179 gemergten Concurrency-Gruppen bleiben erhalten: ältere Pull-Request-Läufe derselben PR- und Workflowklasse können konvergieren, während Push- und Merge-Group-Läufe getrennt bleiben.
3. Die Main-Push-Revalidierung ermittelt vollständig und paginiert die offenen PRs sowie deren hinzugefügte oder umbenannte `registry/tasks/*.json`-Dateien.
4. Im erfolgreichen Pfad wird `registry-registration-preflight/freshness` nur für tatsächliche Registrierungsallokationskandidaten neu erzeugt. Nichtkandidaten erhalten keinen neuen CheckRun und keine Registry-Allokationsprüfung.
5. Fehlerhafte Dateiabfragen und Kandidaten ohne lesbare Head-Repository-Bindung erzeugen vor dem Abbruch einen fehlgeschlagenen Freshness-Check auf dem exakt bekannten PR-Head. Eine unvollständige Kandidatenermittlung markiert zusätzlich alle bereits eindeutig erkannten Kandidaten als fehlgeschlagen.

## Fail-closed-Bindung

Der Kandidatenpfad ist an den am 29. Juli 2026 live gelesenen Branchschutz von `main` gebunden: `required_status_checks.strict=true`; erforderlich sind weiterhin `validate (3.10)`, `validate (3.12)` und `registry-registration-preflight/freshness`. Nach einem neuen Main-Commit ist ein nicht aktualisierter Pull Request damit unabhängig von einem älteren grünen Check nicht mergefähig. Sobald sein Head aktualisiert wird, erzeugt `pull_request_target` den erforderlichen Freshness-Check auf dem neuen Head.

Dieser Pull Request verändert den Branchschutz nicht. Falls `strict` deaktiviert oder ein erforderlicher Check entfernt wird, ist die kandidatenbeschränkte Main-Push-Revalidierung nicht mehr durch diese Evidenz als fail-closed belegt und muss neu bewertet werden.

## Gemessener Effekt

Live-Readback am 29. Juli 2026, 19:23 UTC:

| Messachse | Vorher auf `f46ff846fa9f` | Nachher für denselben Bestand |
| --- | ---: | ---: |
| offene PRs | 5 | 5 vollständig paginiert |
| Added/Renamed-Registry-Task-Kandidaten | 1 (`#1187`) | 1 Freshness-CheckRun und 1 Allokationsprüfung |
| Nichtkandidaten | 4 (`#1184`, `#1185`, `#1186`, `#1188`) | 0 neue Freshness-CheckRuns |
| pauschale Main-Push-Freshness-CheckRuns | 5 | 1 |
| im gebundenen Bestand vermiedene leichte CheckRuns pro Main-Push | 0 | 4 |
| unmittelbar stornierbare bereits laufende Validate-/Preflight-Läufe | 0 beobachtet | keine Hochrechnung |

Die vier vermiedenen CheckRuns sind eine revisionsgebundene Bestandsmessung, keine Aussage über zukünftige Ereignisraten oder GitHub-Kosten.

## Sicherheitsgrenzen

Die Änderung verleiht keine Review-, Merge-, Queue-, Claim-, Dispatch-, Deployment- oder Cleanup-Autorität. Branchschutz, Merge Queue, Codex-Cloud-Einstellungen, Checknamen und Python-Matrix bleiben unverändert. Nicht vollständig prüfbare Kandidaten werden nicht als erfolgreich behandelt; bekannte betroffene Heads erhalten einen blockierenden Fehlercheck, und der Main-Lauf endet fehlgeschlagen.
