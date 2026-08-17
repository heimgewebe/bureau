# Operator Integration Loop v1

Status: active plan

Datum: 2026-07-10

## These, Antithese, Synthese

**These:** Das Operator-Ökosystem besitzt die wesentlichen Organe bereits: Grabowski kann typisiert und quittungspflichtig handeln, WGX kann Repos prüfen, Bureau kann Aufgaben ordnen, RepoBrief kann Kontext liefern, Chronik kann Ereignisse halten und Vibe-Lab kann Arbeitsweisen vergleichen.

**Antithese:** Die vorhandenen Pfade sind noch nicht als geschlossener Arbeitskreislauf verbunden. Ein teurer Coding-Agent als dauerhafter Mutationsoperator wäre ebenso falsch wie ein autonomes lokales Modell: Kosten, Qualität, Datenschutz und Plattformverfügbarkeit schwanken; außerdem darf ein Modelloutput keine Ausführungs- oder Wahrheitsautorität erhalten.

**Synthese:** Grabowski bleibt die auditierbare Mutations- und Ausführungsschicht. KI-Systeme werden nur als austauschbare Fallback-Backends für eng begrenzte Patch- oder Diagnoseartefakte eingesetzt. Der Regelpfad bleibt typisierte Ausführung. Der Kreislauf wird über einen einheitlichen Prüfvertrag, eine kleine Ereignis-Outbox, frische RepoBrief-Provenienz, eine belastbare Bureau-Ready-Lane, einen zunächst rein beobachtenden Nachtlauf und messbare Vibe-Lab-Versuche geschlossen.

## Leitentscheidung zur letzten Meile

Die kosteneffiziente Reihenfolge lautet grundsätzlich:

1. deterministische Repo-Tools und typisierte Grabowski-Grips,
2. lokale KI für kleine, klar begrenzte Patchentwürfe,
3. Gemini Flash über AGY für günstige externe Patch- oder Diagnoseentwürfe,
4. Claude Code oder Codex als Eskalation für komplexe oder qualitätskritische Fälle.

Diese Reihenfolge ist **kein Qualitätsurteil ohne Messung**. Sie ist eine Routing-Hypothese. Jedes Modellbackend erzeugt nur ein begrenztes Artefakt, vorzugsweise einen Patch mit Basis-Hash, Annahmen und vorgeschlagenen Prüfungen. Grabowski prüft Ziel, Hash, Ressourcen-Lease und Verifikationsvertrag und führt die Änderung aus. Kein Modellbackend erhält allein Push-, Merge-, Deploy-, Queue- oder Runtime-Autorität.

## Alternative Sinnachse

- Wird **Kostenminimierung** am höchsten gewichtet, sollen lokale Modelle und Gemini Flash früh versucht werden; mehr Nachprüfung und gelegentliche Eskalation werden akzeptiert.
- Wird **Ergebnisqualität** am höchsten gewichtet, darf die Route früher zu Claude Code oder Codex eskalieren.
- Wird **Datenschutz** am höchsten gewichtet, bleiben sensible oder breite Kontexte lokal; externe Modelle erhalten nur redigierte, hash-gebundene Ausschnitte.
- Wird **Determinismus** am höchsten gewichtet, wird ganz auf KI-Ausführung verzichtet und nur mit Repo-Tools, Tests und Grabowski-Grips gearbeitet.

Die Route gilt nur, wenn Eingabekontext, Patchumfang, Risikoklasse und Prüfbarkeit zur gewählten Stufe passen.

## Historische Ausgangslage, keine Live-Wahrheit

Der Plan wurde am 2026-07-10 aus einem damals frisch geprüften Snapshot abgeleitet. Die dabei beobachteten Task-Zahlen, Dienstzustände und Runtime-Metriken werden hier bewusst **nicht** weiterkopiert oder von Hand aktualisiert. Ein aktiver Plan darf keine zweite Live-Projektion neben den kanonischen Quellen pflegen.

Für aktuelle Entscheidungen gilt stattdessen:

- dauerhafte Taskdefinitionen, Acceptance und Registry-Anker werden direkt aus der kanonischen Registry gelesen;
- aktuelle Runtime-Autoritäts-TaskSpec, Revision und Zustand kommen aus dem autoritativen Bureau-StateStore; ein installierter Registry-Snapshot allein reicht dafür nicht aus;
- Claim-, Run- und Lease-Zustand wird über die koordinierte Bureau-/Grabowski-Laufzeit frisch gelesen;
- PR-, Merge- und CI-Zustand bleibt GitHub-Autorität;
- installierter Bureau-Commit und Runtime-Identität folgen dem Deployment-Manifest und dem Runtime-Refresh-Vertrag;
- Repo-Kontext gilt nur mit quellengebundener Frischeevidenz.

Die konkreten Beobachtungen vom 2026-07-10 bleiben über die Git-Historie nachvollziehbar, besitzen aber keine operative Autorität für spätere Läufe. Die aktuelle Autoritätsaufteilung ist in [`bureau-runtime-refresh-v1.md`](../bureau-runtime-refresh-v1.md) und die Beobachtungsgrenze in [`bureau-runtime-observation-v1.md`](../bureau-runtime-observation-v1.md) dokumentiert. Damit bleibt dieser Plan an der kanonischen Wahrheit verankert, ohne volatile Zustände zu duplizieren.

## Nicht-Ziele

- Keine GPU-, Embedding- oder allgemeine lokale-KI-Infrastrukturarbeit in dieser Initiative.
- Kein Claude-Code-, Gemini-, Codex- oder Ollama-Monopol.
- Kein direkter Push, Merge oder Deploy durch ein Modellbackend.
- Kein zweites Verifikationskommando neben `wgx validate`.
- Kein Chronik- oder Plexer-Dienst allein um der Architektur willen.
- Kein nächtlicher Pull, Branch-Wechsel oder Patch in aktiven Arbeitsverzeichnissen.
- Keine automatische Beförderung einer Route nach einem einzelnen erfolgreichen Versuch.

## Phasen – für Dummies erklärt

Eine **Phase** ist hier ein überprüfbarer Ausbauabschnitt. Jede Phase liefert etwas Nutzbares, bevor die nächste darauf aufbaut. So wird nicht ein großer autonomer Apparat auf einmal eingeschaltet.

### Phase 1 – Fallback-Regeln und Executor-Registry

Wir legen fest, welcher Helfer welche Art von Arbeit versuchen darf. Lokale KI und Gemini Flash erzeugen zunächst nur Patchartefakte. Grabowski bleibt die Hand, die den Patch kontrolliert anwendet. Claude Code und Codex bleiben Eskalationswege.

Ergebnis: eine kosten- und risikobewusste Routingregel mit einheitlichem Receipt.

### Phase 2 – Ein Prüfknopf

`wgx validate` erhält mindestens die Profile `--quick` und `--full` sowie einen stabilen JSON-Receipt. WGX ruft repo-eigene Prüfungen auf, statt Cargo, Pytest, npm, Make oder Just nachzubauen.

Ergebnis: Jeder Agent kann in kurzer Zeit feststellen, ob seine Änderung offensichtlich falsch ist; der vollständige Pfad bleibt für Merge- und CI-Nachweise verfügbar.

### Phase 3 – Kleine Ereignisspur

Grabowski-Tasks und Agenten schreiben nur hochwertige Ereignisse in eine lokale append-only Outbox. Erst wenn Produzenten, Schema, Redaktion und ein echter Consumer belegt sind, wird über einen dauerhaften Chronik-Dienst entschieden.

Ergebnis: Die Arbeit hinterlässt eine auswertbare Spur, ohne sofort zwei neue Dienste zu betreiben.

### Phase 4 – Frischer Repo-Kontext

RepoBrief-Bundles erhalten zwingend Source-Commit, Dirty-State und Erzeugungszeit. Tote Legacy-Registry-Zeilen werden getrennt behandelt. Nach Merge wird der betroffene RepoBrief zeitversetzt erneuert; ein Nachtlauf holt nur verpasste oder veraltete Bundles nach.

Ergebnis: „aktueller RepoBrief“ wird ein belegbarer Zustand statt einer Vermutung.

### Phase 5 – Arbeit wirklich abholbar machen

Bureau erhält eine belastbare Ready-Lane. Promotion, Claim, Lease, Repo-/PR-/Worktree-Kollision und Abschlussreceipt werden klar getrennt. Flüchtige Claims dürfen gitlos sein; dauerhafte Aufgabe und Ergebnis bleiben in Bureau beziehungsweise Git belegt.

Ergebnis: Ein Agent kann die nächste erlaubte Aufgabe atomar übernehmen, ohne dass zwei Threads dieselbe Arbeit beginnen.

### Phase 6 – Nächtlich beobachten, noch nicht autonom reparieren

Der Rechner prüft nachts Repos, schnelle Validierung, RepoGround-Frische, fehlgeschlagene Dienste und wiederkehrende Friction. Zusätzlich liest er `grabowski_audit_projection`: eine verifizierte, read-only Ableitung der Audit-Kette mit festen 24-Stunden-, 7-Tage-, 30-Tage- und Gesamtfenstern. Die Projektion liefert Trends und proposal-only Muster, ersetzt aber weder Runtime Health, Task Store, Live-Leases, Friction-Closeouts noch Execution-Governor-Outcomes.

Der morgendliche Digest dedupliziert unveränderte Befunde mit `findings_sha256`. `source_binding.snapshot_sha256` bleibt die exakte Quellenidentität und verhindert keine Deduplizierung, wenn nur der Audit-Kopf fortschreitet. Der vollständige `projection_sha256` bleibt der zeitgebundene Receipt der einzelnen Abfrage. Ein wachsender Audit-Kopf, fehlende Primärdaten oder eine ungültige Kette werden ausdrücklich als degradierter Zustand gemeldet und nicht zu Grün interpoliert. Der erste produktive Stand mutiert keine Repos und legt nicht automatisch neue Tasks an.

Ergebnis: Der PC liefert morgens einen belegten Gesundheits- und Handlungsüberblick, ohne im Schlaf unerwartet umzubauen.

### Phase 7 – In Vibe-Lab messen

Vibe-Lab vergleicht reale, ausreichend ähnliche Arbeiten: deterministisch/Grabowski, lokale KI, Gemini Flash über AGY und bei Bedarf Claude Code oder Codex. Gemessen werden Kosten, Laufzeit, Patchumfang, Prüfpassung, Reviewfunde, Rework und entkommene Fehler.

Ergebnis: Die Routingregel wird aufgrund von Evidenz angepasst oder verworfen, nicht aufgrund von Modellimage oder Einzelanekdoten.

## Systemgrenzen

| Organ | Autorität in diesem Plan |
|---|---|
| Bureau | dauerhafte Aufgabe, Priorität, Abhängigkeit und Abschlusswahrheit |
| Grabowski | Ressourcen-Leases, typisierte Ausführung, Mutation und Receipt |
| WGX | gemeinsamer Verifikationsrouter, nicht Task- oder Deploy-Owner |
| RepoBrief/Lenskit | quellengebundener Kontext und Frischebeleg, keine Mutation |
| Chronik | zusätzliche Ereignishistorie, keine Queue- oder Command-Autorität |
| Vibe-Lab | Messung und Vergleich, keine automatische Promotion |
| lokale KI / Gemini Flash / Claude Code / Codex | austauschbare Hilfsbackends, keine eigene Systemautorität |
| Leitstand | abgeleitete Anzeige, keine Primärwahrheit |

## Abhängigkeiten und Wiederverwendung

Diese Initiative dupliziert bestehende Arbeit nicht:

- `GRIP-ROADMAP-V1-T016` liefert die Captain-Executor-Registry-Grundlage.
- `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T005` definiert Consumer-, Metrik-, Oberflächenbudget- und Ablaufpflicht für Vibe-Lab-Experimente.
- `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T006` grenzt hochwertige Chronik-Ereignisse ein.
- `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T008` konsolidiert die tägliche RepoBrief-Artefaktfläche.
- `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T009` schärft WGX auf tatsächlich gemeinsame Prüfungen.
- `CABINET-GEMINI-MAINT-V1-T005` entscheidet separat über eine wiederkehrende Cabinet-Gemini-Wartung; daraus folgt keine allgemeine Executor-Freigabe.
- `RPU-V1-T012` und `RPU-V1-T021` liefern das Muster für konservative Vibe-Lab-Auswertung ohne automatische Promotion.
- `GRABOWSKI-OPERATOR-SURFACE-V1-T077` liefert die verifizierte Audit-Projektion; T006 konsumiert sie nur als abgeleitete, proposal-only Beobachtung.

## Risiken und Gegenmaßnahmen

| Risiko | Folge | Gegenmaßnahme |
|---|---|---|
| billiges Modell erzeugt schlechte Patches | Rework oder verdeckte Fehler | kleine Patchgrenze, Basis-Hash, Quick/Full-Validate, Review und Eskalation |
| Executor-Fallback wird zum Schattenoperator | unklare Autorität | Modell erzeugt Artefakt; Grabowski wendet an und quittiert |
| WGX dupliziert Repo-CI | zusätzliche Pflege und widersprüchliche Ergebnisse | WGX routet deklarativ zu repo-eigenen Frontdoors |
| Ereignisspur wird Log-Müll | Speicher- und Erkenntnisverlust | kleine Allowlist, Consumerpflicht, Retention und Redaktion |
| Nachtlauf wird autonome Control-Plane | unerwartete Änderungen | erste Version observe-and-diagnose-only; Mutation als eigener späterer Freigabeentscheid |
| Bureau-Claims landen wieder in Git | Mergekonflikte und langsame Koordination | flüchtige Lease außerhalb Git, dauerhafte Receipts in Bureau |
| Vibe-Lab misst ungleiche Aufgaben | falsche Routingentscheidung | Taskklassen, Diffgröße, Risikoklasse und externe Beobachtungen binden |

## Abschlusskriterien

Die Initiative ist abgeschlossen, wenn:

1. ein Fallback-Executor-Vertrag lokale KI und Gemini Flash als günstige Artefaktgeneratoren unterstützt, aber Grabowski als Mutationsautorität erhält;
2. `wgx validate --quick|--full --json` oder ein gleichwertiger, nicht redundanter Vertrag für Kernrepos belegt ist;
3. eine kleine, redigierte Ereignis-Outbox mindestens einen realen Consumer versorgt;
4. RepoBrief-Frische durch Source-Commit und Dirty-State bestimmbar ist und Refreshes mergegebunden sowie nachholbar sind;
5. Bureau eine funktionierende Ready-/Claim-/Lease-Kette mit Kollisionsprüfung besitzt;
6. ein nächtlicher Observe-and-Diagnose-Lauf einen belegten Digest erzeugt, ohne Repos zu mutieren;
7. Vibe-Lab mindestens eine vergleichbare Routevaluation liefert und eine explizite `promote`, `pilot`, `defer` oder `drop`-Entscheidung festhält.

## Unsicherheit

Unsicherheit: 0,18. Die historische Ausgangslage war am 2026-07-10 direkt geprüft; aktuelle operative Zustände müssen frisch aus den oben genannten Autoritäten gelesen werden. Kosten- und Qualitätsreihenfolge der Modellbackends bleibt bis zur Messreihe eine Hypothese.

Interpolationsgrad: 0,31. Systemgrenzen sind aus bestehenden Verträgen abgeleitet; konkrete Schwellen für Kosten, Patchgröße und Eskalation müssen empirisch festgelegt werden.
