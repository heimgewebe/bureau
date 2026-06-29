# Bureau Discovery v1 — Quelleninventar und Umsetzungsplan

Status: planned  
Scope: task discovery from repositories, Vault-Gewebe and registered boards  
Non-goal: automatic execution of unverified prose findings

## 1. Ziel

Bureau soll stündlich neue oder veränderte Arbeitsquellen finden, daraus nachvollziehbare
Task-Kandidaten erzeugen, sie mit bestehender Arbeit abgleichen und nur hinreichend belastbare
Kandidaten in die Bureau-Registry übernehmen.

Die Quellen bleiben Eigentümer ihrer Inhalte. Bureau speichert revisionsgebundene Snapshots,
Entscheidungen und Projektionen; es schreibt nicht in Quell-Repositories oder Vault-Dokumente
zurück.

## 2. Inventar

Das initiale Inventar liegt in
`docs/reports/discovery-source-inventory.v1.json`.

Ergebnis der Bestandsaufnahme:

- 47 lokale Haupt-Checkouts unter `~/repos`
- 40 eindeutige Projektquellen nach Remote-Deduplizierung
- 7 abgeleitete oder duplizierte Checkouts, die keine eigenen Tasks erzeugen dürfen
- 24 Projektquellen mit erkennbaren Roadmaps, Boards, Plänen oder Blueprints
- 12 Repo-Quellen mit direkter oder gemeinsamer Projektfläche im Vault-Gewebe

Worktrees, Deployment-Checkouts und derivative Klone sind nur Reconciliation-Evidence. Sie sind
keine Discovery-Quellen.

## 3. Quellenhierarchie

### Tier A — strukturierte, autoritative Task-Quellen

Diese Quellen dürfen deterministische Task-Kandidaten mit hoher Konfidenz liefern:

- `lenskit/docs/tasks/index.json`
- `lenskit/docs/tasks/board.md`
- `weltgewebe/docs/tasks/index.json`
- `weltgewebe/docs/tasks/board.md`
- weitere explizit registrierte Task-Manifeste mit Schema

Ein strukturierter Eintrag wird trotzdem nicht automatisch `ready` oder `verified`. Bureau prüft
Revision, Status, Deduplizierung, Ziel-Repository und bestehende Arbeit.

### Tier B — kanonische Repo-Planung

Roadmaps, Masterpläne, Blueprints, Fahrpläne, `next-steps` und aktive Checklisten in den
kanonischen Haupt-Checkouts. Initial besonders relevant:

- `agent-control-surface/docs/blaupause.md`
- `chronik/docs/PLAN_OPTIMIERUNG.md`
- `device-graph/docs/plans/roadmap.md`
- `device-graph/docs/plans/next-steps.md`
- `grabowski/docs/roadmap.md`
- `hausKI/docs/ist-stand-vs-roadmap.md`
- `heim-assi/docs/HEIM-ASSI-BLUEPRINT.md`
- `heimgeist/docs/heimgewebe-vektor-blaupause.md`
- `heimserver/docs/decisions/0001-adopt-agentic-blueprint.md`
- `icf-tool/roadmap.md`
- `infra/docs/infra/blueprint.md`
- `lenskit/docs/roadmap.md`
- `lenskit/docs/roadmap/lenskit-master-roadmap.md`
- `metarepo/docs/vision/heimgewebe-capability-plan.md`
- `obsidian-bridge/docs/roadmap.md`
- `schauwerk/docs/roadmap.md`
- `semantAH/docs/roadmap.md`
- `sichter/docs/PLAN_SICHTER_AUSBAU.md`
- `sichter/docs/ROADMAP_REVIEWER.md`
- `snippet-engine-control/docs/blaupause.md`
- `spannungsatlas/MASTERPLAN.md`
- `spannungsatlas/docs/roadmap.md`
- `steuerboard/docs/masterplan.md`
- `steuerboard/docs/roadmap.md`
- `vibe-lab/docs/masterplan.md`
- `vibe-lab/docs/roadmap.md`
- `weltgewebe/docs/roadmap.md`
- `weltgewebe/docs/process/fahrplan.md`

Tier-B-Dokumente erzeugen zunächst Discovery-Kandidaten. Automatische Promotion ist nur bei
expliziter ID, offenem Status, eindeutigem Zielzustand und überprüfbaren Akzeptanzkriterien
zulässig.

### Tier C — Vault-Gewebe-Projektflächen

Vault-Dokumente sind Absichts- und Ideenspeicher. Repo-Wahrheit hat Vorrang. Initiale Allowlist:

| Repository | Vault-Gewebe-Pfad | Rolle |
|---|---|---|
| agent-control-surface | `agent-control-surface/` | Projektplanung |
| heimserver | `heimserver/` | Projektplanung |
| icf-tool | `icf-tool/` | Projektplanung |
| leitstand | `leitstand/` | Projektplanung |
| lenskit | `lenskit/` | Projektplanung |
| obsidian-bridge | `obsidian/bridge/` und `obsidian/` | Projektplanung |
| snippet-engine-control | `snippet-engine-control/` | Projektplanung |
| spannungsatlas | `spannungsatlas/` | Projektplanung |
| weltgewebe | `weltgewebe/` | Projektplanung |
| heimgeist | `heimgewebe/` | gemeinsame Heimgewebe-Planung |
| leitwerk | `heimgewebe/` | gemeinsame Heimgewebe-Planung |
| metarepo | `heimgewebe/` | gemeinsame Heimgewebe-Planung |

Vault-Kandidaten benötigen immer Quellpfad, Git-Revision oder Dateihash, relevante Textstelle,
Ziel-Repository und einen Deduplizierungsfingerprint.

### Tier D — ergänzende Kontrollquellen

Diese Quellen dürfen Kandidaten bestätigen, blockieren oder als erledigt markieren, aber nicht
allein ausführbare Tasks erzeugen:

- offene GitHub-Issues
- offene Pull Requests und aktuelle CI-Ergebnisse
- registrierte Schauwerk-/Miro-Boards
- `vault-gewebe/Observatorium/_machine/`
- explizit freigegebene Obsidian-Canvas-Dateien
- Bureau-, Grabowski- und Operator-Receipts
- Steuerboard-Readiness und Cabinet-Entscheidungsreferenzen

## 4. Repository-Abdeckung

Standardmäßig aktivierte eindeutige Repo-Quellen:

`agent-control-surface`, `aussensensor`, `bureau`, `cabinet`, `chronik`, `contracts-mirror`,
`device-graph`, `grabowski`, `hausKI`, `hausKI-audio`, `heim-assi`, `heim-pc`, `heimgeist`,
`heimlern`, `heimserver`, `icf-tool`, `infra`, `leitstand`, `leitwerk`, `lenskit`, `metarepo`,
`mitschreiber`, `obsidian-bridge`, `plexer`, `schauwerk`, `semantAH`, `sichter`,
`snippet-engine-control`, `spannungsatlas`, `steuerboard`, `vault-gewebe`, `vibe-lab`,
`weltgewebe`, `wgx`.

Standardmäßig deaktiviert oder nur auf ausdrückliche Freigabe zu scannen:

- `agent-smoke-test` — lokales Test-Repository
- `claim-audit-lab` — Experiment/Labor
- `demo-repository` — Demo
- `hausarbeit` — persönliches Repository
- `PrepP` — persönliches Repository
- `vault-privat` — außerhalb dieses Discovery-Scopes

Derivative Checkouts derselben Remotes werden vollständig ausgeschlossen. Die genaue Liste steht
im Inventarbericht.

## 5. Ausschlüsse

Nie automatisch scannen oder als Task-Quelle verwenden:

- `.git/`, `.venv/`, `node_modules/`, `dist/`, `build/`, `target/`
- `__pycache__/`, `.ruff_cache/`, `.pytest_cache/`
- `.claude/worktrees/`, `lenskit-worktrees/`, `.bureau-worktrees/`
- Deployment-, Reparatur- und PR-Checkouts
- `vault-gewebe/.obsidian/`, `.smart-env/`, `.trash/`, `halde/`
- Archive und Dateien mit `old`, `alt`, `copy`, `Kopie`, `legacy` oder `deprecated`, sofern sie
  nicht ausdrücklich als kanonisch markiert sind
- Binärdateien, PDFs, DOCX und Bilder in Discovery v1
- generierte Artefakte und Beweisberichte als neue Task-Quelle

Beweisberichte dürfen bestehende Kandidaten nur bestätigen oder widerlegen.

## 6. Source Registry

Bureau erhält eine versionierte Source Registry. Jeder Eintrag enthält mindestens:

```json
{
  "source_id": "repo:lenskit",
  "kind": "git-repository",
  "root": "/home/alex/repos/lenskit",
  "remote": "heimgewebe/lenskit",
  "ref": "origin/main",
  "vault_paths": ["/home/alex/repos/vault-gewebe/lenskit"],
  "authority": "structured",
  "include": ["docs/tasks/**", "docs/roadmap/**", "docs/blueprints/**"],
  "exclude": ["docs/archive/**", ".claude/worktrees/**"],
  "promotion_policy": "structured-only"
}
```

Die Registry wird nicht bei jedem Lauf neu erraten. Ein Inventarisierungsbefehl darf Änderungen
vorschlagen, aber nur ein geprüfter Registry-Commit ändert die erlaubten Quellen.

## 7. Kandidatenmodell

Jeder Fund wird zunächst als unveränderlicher Kandidat gespeichert:

```text
candidate_id
source_id
source_revision
source_path
source_anchor
source_content_sha256
project_id
candidate_kind
external_id
status
summary
target_outcome
acceptance
confidence
fingerprint
first_seen_at
last_seen_at
decision
rejection_reason
```

Zustände:

```text
discovered -> confirmed -> accepted -> planned -> ready
           -> duplicate
           -> obsolete
           -> rejected
           -> informational
```

Kein Kandidat überspringt `accepted`. Freie Prosa erreicht niemals automatisch `ready`.

## 8. Extraktion

### Deterministisch in Discovery v1

- JSON-Task-Indizes und deren Schemas
- Markdown-Boards mit stabilen IDs
- YAML-Frontmatter
- Markdown-Checklisten
- Überschriften `Offen`, `Nächste Schritte`, `Folgearbeiten`, `Blocked`
- explizite Statuswerte `open`, `partial`, `blocked`, `planned`
- explizite IDs und Akzeptanzkriterien

### Später und nur als Discovery

- freie Blueprint-Prosa
- Obsidian Canvas
- Miro-Freitext
- semantische Zusammenführung ohne gemeinsame ID
- PDFs und DOCX

LLM-Auswertung darf nur Kandidaten mit exakter Quellenstelle erzeugen. Sie darf keine Registry-
Task direkt schreiben.

## 9. Deduplizierung und Reconciliation

Primärer Fingerprint:

```text
source_id + source_path + local_item_id + normalized_target_outcome
```

Sekundärer Abgleich:

- gleiche externe ID
- gleiches Ziel-Repository
- gleicher oder überlappender Zielzustand
- existierender Bureau-Task
- aktiver Run oder Grabowski-Task
- offener oder gemergter Pull Request
- Receipt, das den Zielzustand bereits belegt
- neuerer kanonischer Plan, der den Fund ersetzt

Repo-Status schlägt Vault-Status. Ein gemergter und belegter Zielzustand darf durch eine ältere
Vault-Notiz nicht wieder geöffnet werden.

## 10. Stündlicher Betrieb

### Zur halben Stunde — Discovery Scanner

- nur geänderte Source-Revisionen scannen
- Snapshots und Kandidaten schreiben
- keine Queue-Reihenfolge verändern
- keine Tasks starten
- immer ein Receipt schreiben

### Zur vollen Stunde — Bureau Operator

1. laufende Runs und externe Bindungen reconciliieren
2. neue Kandidaten einlesen
3. Duplikate, obsolete und erledigte Kandidaten markieren
4. bestätigte Kandidaten priorisieren
5. höchstens zwei neue `planned` Tasks pro Lauf aufnehmen
6. höchstens einen Task nach bestandener Readiness zu `ready` promovieren
7. höchstens einen konfliktfreien Task starten
8. immer ein Receipt schreiben, auch bei Leerlauf

Grenzen pro Lauf:

```text
max_new_candidates = 20
max_confirmed_candidates = 5
max_promoted_tasks = 2
max_started_tasks = 1
```

## 11. Priorisierung

Deterministische positive Signale:

- strukturierte autoritative Quelle
- stabile Task-ID
- kanonischer Status `open`, `partial` oder `blocked`
- eindeutiges Ziel-Repository
- Akzeptanzkriterien vorhanden
- bestehende begonnene Arbeit
- Aufgabe beseitigt einen Blocker
- aktuelle Roadmap statt älterer Vault-Notiz

Negative Signale:

- nur vage Idee
- keine Zieldefinition
- keine Zuordnung zu einem Projekt
- ältere oder archivierte Quelle
- aktiver PR oder Run existiert bereits
- Zielzustand bereits belegt
- persönliche oder nicht freigegebene Quelle

## 12. Umsetzungsschritte

### DISC-001 — Source Registry

- Inventarformat und Schema festlegen
- 40 eindeutige Repo-Quellen registrieren
- 7 derivative Checkouts explizit ausschließen
- Vault-Allowlist und globale Kontrollquellen registrieren

Akzeptanz: identische Hostsituation erzeugt byteidentisches Inventar.

### DISC-002 — Change Detector

- Git-Commit pro Repo
- Blob-Hash pro Planungsdatei
- Dateihash für freigegebene Vault-Dateien
- nur geänderte Quellen weiterverarbeiten

Akzeptanz: unveränderte Quellen erzeugen keine neuen Kandidaten.

### DISC-003 — Deterministische Extractors

- Task-Index
- Markdown-Board
- Frontmatter
- Checklisten und Statusabschnitte

Akzeptanz: Extraktion ist ohne LLM reproduzierbar und quellengebunden.

### DISC-004 — Discovery Inbox

- persistente Kandidaten und Entscheidungen
- stabile Fingerprints
- `duplicate`, `obsolete`, `rejected`, `informational`
- unveränderliche Source-Snapshots

Akzeptanz: ein abgelehnter Fund erscheint nicht stündlich erneut.

### DISC-005 — Cross-source Reconciliation

- Repo gegen Vault
- Kandidat gegen Bureau-Task
- Kandidat gegen Run, PR, Receipt und Steuerboard
- neuere Quelle gewinnt nach definierter Autoritätsordnung

Akzeptanz: bereits gemergte Arbeit wird nicht wieder eröffnet.

### DISC-006 — Operator Integration

- Halb-Stunden-Scanner auf Discovery umstellen
- Stunden-Operator mit Kandidatenprüfung erweitern
- Limits und Receipts erzwingen
- historische Transient-Unit-Fehler aus aktuellen Findings entfernen

Akzeptanz: jeder Lauf besitzt ein Receipt und erzeugt höchstens die festgelegte Anzahl Tasks.

### DISC-007 — Pilot

Pilotprojekte:

1. `lenskit` — strukturierter Task-Index plus Vault-Ordner
2. `weltgewebe` — strukturierter Task-Index plus Vault-Ordner
3. `spannungsatlas` — Roadmap/Masterplan plus Vault-Ordner
4. `grabowski` — Repo-Roadmap ohne Vault-Paar

Testfälle:

- gleicher Task in Repo und Vault
- erledigter Repo-Task, aber offene alte Vault-Notiz
- neuer Roadmap-Punkt mit stabiler ID
- vage Blueprint-Idee ohne Akzeptanz
- aktiver PR für denselben Zielzustand
- unveränderte Quellen im Folgelauf

Akzeptanz: keine Duplikate, keine Wiedereröffnung erledigter Arbeit, keine automatische Ausführung
freier Prosa.

## 13. Reihenfolge

```text
Source Registry
-> Change Detector
-> strukturierte Extractors
-> Discovery Inbox
-> Reconciliation
-> stündliche Integration
-> Pilot
-> erst danach semantische Blueprint-/Canvas-Auswertung
```

Diese Reihenfolge hält Bureau klein und deterministisch. Der Dispatcher bleibt unverändert; die
neue Discovery-Schicht liefert ihm ausschließlich geprüfte, revisionsgebundene Eingaben.
