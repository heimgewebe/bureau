# Bureau Control Plane v3

## Ziel

Bureau wird lokal-first, transaktional, offlinefähig und multi-agentensicher:

- GitHub `main` bleibt öffentliche Autorität für Code, Schemas, Reviews, CI und Releases.
- Der lokale Bureau-StateStore wird alleinige operative Autorität für Taskrevisionen, Status, Frontier, Claims, Runs, Acceptance und Closeout.
- Grabowski bleibt Autorität für Prozesse, konkrete Ressourcenleases, Workspaces, Host-, Git-, Netzwerk- und Deploymentwirkungen.
- Redigierte GitHub-Snapshots dienen Transparenz und Prüfbarkeit, besitzen aber keine Rückschreibautorität.

## Ausgangslage

Der Umbau beginnt nicht bei null. Auf dem aktuellen Bureau-Stand existieren bereits:

- SQLite mit WAL, `synchronous=FULL` und `BEGIN IMMEDIATE`,
- Tabellen für Events, Runs, Claims, Ressourcen, Envelopes und Receipts,
- idempotente und CAS-gebundene Teilpfade,
- ein gitloses Live Register,
- immutable Runtime- und Registry-Bindung,
- lokal autoritative Queue-Nachfüllung.

Control Plane v3 ist deshalb eine Migration und Konvergenz dieser Organe. Eine zweite Datenbank, ein zweiter Workflow-Kern oder Hosted-Runner-Autorität sind ausgeschlossen.

## Autoritätsmodell

| Gegenstand | Autoritative Quelle |
|---|---|
| Code, Schemas, Migrationscode | GitHub `main` |
| TaskSpec und TaskSpec-Revision | Bureau StateStore |
| Task-, Initiativen- und Frontierzustand | Bureau StateStore |
| Claims, Runs, Acceptance und Receipts | Bureau StateStore |
| Prozesse, Workspaces und konkrete Leases | Grabowski |
| PR, Branch, Review und CI | GitHub |
| Deployment- und Livezustand | Grabowski und Zielruntime |
| Vollbackup | verschlüsseltes Restic-Backup |
| öffentliche Übersicht | redigierter, hashgebundener Snapshot |

## Harte Invarianten

1. Keine operative Status-, Queue-, Lifecycle- oder Closeoutmutation benötigt einen PR.
2. Es gibt zu jedem Zeitpunkt genau einen autoritativen Writer.
3. Jede Mutation ist idempotent, revisionsgebunden, CAS-gesichert und besitzt terminalen Readback.
4. Kein Workspace oder Prozess entsteht vor vollständiger Owner- und Ressourcenbindung.
5. `unknown` wird weder als Erfolg noch als Fehler interpretiert.
6. Hosted Runner validieren angelieferte Artefakte; sie berechnen keine lokale Laufzeitwahrheit.
7. GitHub-Ausfall blockiert Intake, Claim, Ausführung und Closeout nicht.
8. Fremde Dirty-States, Leases, Prozesse, Branches und Worktrees bleiben geschützt.
9. Ein Snapshot ersetzt kein verschlüsseltes Backup.
10. Cutover erfolgt erst nach erfolgreichem Schattenbetrieb und belegtem Restore.

## Ausführungsreihenfolge

| Task | Inhalt | Abhängigkeit |
|---|---|---|
| T001 | Autoritäts- und Consumerinventar | – |
| T002 | Übergangsjournal und Replay | T001 |
| T003 | TaskSpec-Revisionen im StateStore | T002 |
| T004 | dynamische Frontier statt manueller Queue | T003 |
| T005 | atomare Mutations-, Claim- und Dispatch-API | T002, T003 |
| T006 | automatische Lifecycle-Reconciliation | T003, T005 |
| T007 | Typed Acceptance und Closeout | T005, T006 |
| T008 | vereinheitlichter Candidate-Intake | T003, T005 |
| T009 | redigierte öffentliche Snapshots | T003, T005 |
| T010 | Backup, Restore und Replaytest | T002 |
| T011 | Doctor und Leitstand | T004–T010 |
| T012 | Schattenbetrieb und Cutover | T011 |
| T013 | alte Git-Schreibpfade entfernen | T012 |

Die Initiative begrenzt sich auf einen aktiven Writer im Bureau-Repository. Andere Repositories und disjunkte read-only Lanes dürfen parallel weiterarbeiten.

## Cutover-Stufen

### A – Git bleibt Autorität

StateStore importiert und projiziert. Alle neuen Projektionen werden mit den Git-Dateien verglichen. Es entsteht keine neue Wirkung.

### B – StateStore wird Writer

Task-, Status- und Frontieroperationen schreiben ausschließlich in den StateStore. Git-Dateien werden nur noch daraus generiert. Jede Divergenz blockiert Publikation und Cutover.

### C – Git-Projektion wird optional

Alle produktiven Consumer lesen StateStore oder dessen begrenzte API. `registry/queue.json` und Taskdateien sind nur noch Kompatibilitäts- und Archivprojektionen.

### D – Legacy-Pfade entfernen

Queue-, Status-, Lifecycle-, Closeout- und Taskregistrierungs-PR-Pfade werden entfernt. Code-PRs, öffentliche CI, Releases und redigierte Snapshots bleiben.

## Cutover-Gates

- vollständiges Consumerinventar,
- keine unbekannten Git- oder State-Schreiber,
- wiederholte Null-Divergenz,
- erfolgreicher Backup- und Restoretest,
- atomarer Mehragenten-Claimtest,
- automatischer Acceptance-Closeout,
- Offline-Betrieb ohne GitHub,
- Runtime- und Snapshot-Readback,
- getesteter Rückrollpfad auf exakten Checkpoint.

## Zielmetriken

| Kennzahl | Ziel |
|---|---:|
| Queue-/Status-/Closeout-PRs | 0 |
| manuelle Queuekorrekturen | 0 |
| terminale Tasks in ausführbaren Lanes | 0 |
| doppelte Claims | 0 |
| ownerlose Workspaces | 0 |
| Tasks mit vollständiger Resource-Bindung | 100 % |
| automatischer Closeout | innerhalb eines Reconcile-Zyklus |
| StateStore-/Projektiondivergenz | 0 |
| Restoretests | regelmäßig grün |
| GitHub-Ausfall | kein lokaler Betriebsstillstand |

## Rückrollgrenze

Bei unerklärter Divergenz, fehlender Restorefähigkeit, ungebundenem Writer, Acceptance-Fehlklassifikation oder unbekanntem Mutationsausgang wird kein Cutover fortgesetzt. Die letzte kohärente Release-, Schema-, Ereignis- und Receipt-Wurzel bleibt autoritativ; fremde Arbeit wird nicht verändert.
