# RepoBrief Agent Code Empowerment v1

Registered: 2026-07-07

## Ziel

RepoBrief ist ausschließlich für Agenten. Der Zweck ist nicht, Menschen ein schönes Repo-Dossier zu geben, sondern Agenten so auszustatten, dass sie mit Code besser umgehen können:

- schneller die relevanten Codeflächen finden;
- Änderungen kleiner und begründeter planen;
- Risiken, unbekannte Live-Zustände und Nicht-Beweise ausdrücklich sehen;
- Patch- und Testarbeit sauber an externe Ausführungsflächen übergeben;
- Ergebnisse aus Patch-Evaluation, CI und Review nicht mit Wahrheit verwechseln.

## Vorherige Prüfung: Ist dieser Plan sinnvoll?

### These

Ein agent-only RepoBrief braucht eine stärkere Code-Workbench-Achse. Snapshot, Reading Pack, Citation Map und Health helfen beim Lesen, aber sie reichen für Codearbeit nur dann, wenn sie in konkrete Agentenhandlungen übersetzt werden: lokalisieren, verstehen, Impact abschätzen, Änderung planen, Evaluationsbedarf bestimmen.

### Antithese

Ein neuer Optimierungsplan kann schaden, wenn er bestehende RBV1- und RBAW-Aufgaben dupliziert oder RepoBrief zur Mutationsmaschine ausbaut. Genau das wäre gegen die bestehende Boundary: RepoBrief darf Evidenz projizieren und lesen, aber keine Patches anwenden, keine Shell-/Testschleifen als Urteil ausführen und keine Merge- oder Korrektheitsfreigabe erzeugen.

### Synthese

Der Plan ist sinnvoll, wenn er als dünne Integrationsschicht über bestehenden Flächen registriert wird:

- bestehende Roadmap-Slices bleiben Eigentümer für CLI, MCP, Graph, Symbol Index, Retrieval und Patch Evaluation;
- diese Initiative definiert die agentische Code-Handlungslogik darüber;
- mutation, Tests und Sandbox bleiben außerhalb von RepoBrief Core;
- jede Agentenentscheidung bleibt an Evidenz, Freshness und explizite Nicht-Claims gebunden.

## Alternative Sinnachse

Wenn das Ziel menschliche Lesbarkeit wäre, müsste RepoBrief vor allem Einstieg, Übersicht und Narrativ optimieren. Da das Ziel ausschließlich Agentenempowerment ist, zählt etwas anderes:

1. Welche Codeaufgabe liegt vor?
2. Welche Evidenz braucht ein Agent dafür?
3. Welche Live-Zustände fehlen im Snapshot?
4. Welche Änderung ist minimal plausibel?
5. Welche externe Evaluation ist nötig?
6. Welche Aussagen darf das Ergebnis nicht stützen?

## Belegt / plausibel / spekulativ

### Belegt im aktuellen Arbeitskontext

- Es existiert bereits eine RepoBrief-v1-Roadmap mit Snapshot-, Profile-, Health-, Access-, CLI-, MCP-, Graph-, Symbol- und Retrieval-Slices.
- Es existiert bereits eine Agent-Workbench-Boundary: deterministische read-only Codeverständniswerkzeuge dürfen zu RepoBrief gehören; Patch-/Test-/Sandboxautorität bleibt extern.
- Patch-Evaluation-Contract und read-only Consumption wurden bereits als verifizierte RBAW-Slices registriert.
- Mehrere noch offene RBV1-Slices betreffen direkt Agenten-Codearbeit: Relation Goldset, Graph Availability, Python AST Symbol Index und Retrieval-v2-Evaluation.

### Plausibel

- Agenten werden stärker durch task-spezifische Evidenzanforderungen und Impact Maps entlastet als durch weitere allgemeine Dokumentation.
- Ein maschinenlesbarer Agent Change Plan reduziert große, schlecht begründete Patches.
- Ein Evaluation-Loop mit Miss Taxonomy verbessert RepoBrief stärker als bloßer Ausbau von Artefakten.

### Spekulativ

- Symbol-, Referenz- und Impact-Flächen werden die reale Patchqualität deutlich erhöhen. Das muss gemessen werden.
- MCP-Ressourcen für Codeaufgaben werden produktiver sein als CLI-only-Nutzung. Das hängt vom Agenten-Client ab.

## Prämissencheck

Die Initiative gilt, wenn:

- RepoBrief wirklich Agenten-Infrastruktur bleiben soll;
- Codearbeit als Hauptfall wichtiger ist als menschliches Repo-Onboarding;
- Read-only Boundary und Nicht-Claims nicht aufgeweicht werden;
- bestehende RBV1/RBAW-Slices nicht dupliziert, sondern integriert werden.

Sie gilt nicht, wenn:

- RepoBrief zur allgemeinen menschlichen Dokumentationsschicht werden soll;
- Patchausführung in RepoBrief Core gewünscht ist;
- schnelle Feature-Umsetzung wichtiger ist als sichere Evidenzführung;
- eine einzelne UI- oder CLI-Verbesserung gerade wichtiger ist als Agentenhandlungsqualität.

## Zielarchitektur

RepoBrief für Agenten besteht aus fünf Schichten:

1. **Evidence Substrate**: Snapshot, Canonical Brief Source, Manifest, Citation Map, Chunk Index, Health, Freshness, Availability.
2. **Code Understanding Surfaces**: Symbol Index, Reference Index, Import/Call/Test/Contract Relations, Graph Availability, Range Resolution.
3. **Agent Task Evidence Matrix**: Zuordnung von Codeaufgaben zu erforderlichen Evidenzflächen und Nicht-Beweisen.
4. **Agent Change Plan**: maschinenlesbarer Plan vor Patch: Ziel, betroffene Dateien, Belege, Risiken, erwartete Checks, Live-Lücken.
5. **External Evaluation Loop**: Patch Evaluation Sidecar, CI, Review und Bureau-Receipt als externe Evidenz, nie als Wahrheitsautomat.

## Nicht-Ziele

Diese Initiative baut nicht:

- keine Patchanwendung in RepoBrief Core;
- keine Shell-/Testausführung als RepoBrief-Urteil;
- keine Auto-Merge- oder Review-Freigabe;
- keine `supported/unsupported`-Wahrheitsmaschine;
- keine LLM-Reranking-Abhängigkeit im deterministischen Kern;
- keine neue Großarchitektur parallel zu RBV1/RBAW.

## Optimierungsplan

### Phase 1 — Agent Code Task Evidence Matrix

Definiert eine Matrix für Agenten-Codeaufgaben:

- Bugfix;
- Refactor;
- Feature-Add;
- API/Contract-Change;
- Test-Add/Repair;
- PR Review;
- Security-sensitive Change;
- Documentation-bound Code Change.

Für jede Aufgabe werden festgelegt:

- required evidence;
- recommended evidence;
- forbidden inference;
- live checks needed;
- does_not_establish;
- minimal answer/change obligations.

### Phase 2 — Agent Change Plan Contract

Vor einem Patch erzeugt der Agent einen kleinen strukturierten Plan:

- task_kind;
- target behavior;
- candidate files/ranges with citations;
- suspected impact surfaces;
- expected tests/checks;
- unknown live state;
- rollback or stop condition;
- non-claims.

Der Plan ist keine Freigabe und kein Patch. Er ist die Brücke zwischen RepoBrief-Evidenz und externer Patch-Evaluation.

### Phase 3 — Code Impact Map v1

RepoBrief soll Agenten zeigen, was eine Änderung wahrscheinlich berührt:

- symbol definitions;
- references;
- imports;
- tests by name/path;
- contracts/schemas;
- CLI/MCP/API entrypoints;
- docs/runbooks;
- known risky boundaries.

Die Impact Map darf nur Navigation und Risikooberfläche sein. Sie behauptet keine vollständige Abdeckung.

### Phase 4 — Required Reading für Codeänderungen

Bestehende Required-Reading- und Preflight-Flächen werden um code-change-task profiles erweitert:

- `code_bugfix`;
- `code_refactor`;
- `code_feature`;
- `code_contract_change`;
- `code_pr_review`;
- `code_security_sensitive`.

Jedes Profil verlangt andere Evidenz und andere Live-Checks.

### Phase 5 — MCP/CLI Agent Workbench Surface

Agenten brauchen abrufbare, kleine Flächen statt großer Dumps:

- `code_task_evidence.resolve`;
- `impact_map.get`;
- `symbol.get`;
- `references.get`;
- `tests_for_target.get`;
- `change_plan.validate`;
- `live_gap.report`.

MCP/CLI bleiben read-only gegenüber Repo, Git und Shell. Snapshot-Erzeugung bleibt explizit; Patch/Test bleibt extern.

### Phase 6 — Agent Outcome Evaluation Loop

RepoBrief muss messen, ob Agenten wirklich besser arbeiten:

- Lokalisationstreffer: richtige Dateien/Ranges gefunden;
- Evidence Completeness: erforderliche Belege vorhanden;
- Patch Scope: Änderung klein genug;
- Evaluation Fit: passende Checks vorgeschlagen;
- Miss Taxonomy: welche Evidenz fehlte oder irreführte;
- Sidecar/CI Outcome: nur externe Beobachtung, kein Wahrheitsurteil.

### Phase 7 — Promotion Gate

Neue Agentenflächen werden nicht default, nur weil sie existieren. Promotion braucht:

- Retrieval-/Localization-Messung;
- keine zentrale Query-Klasse regressiert;
- dokumentierte False Positives;
- Nicht-Claims sichtbar;
- Rückfallpfad auf Canonical Source und klassische Required Reading.

## Sequenzierung gegen bestehende Bureau-Aufgaben

Diese Initiative ersetzt nicht:

- `RBV1-T014` Relation Guard Goldset;
- `RBV1-T015` Graph Availability;
- `RBV1-T016` Python AST Symbol Index;
- `RBV1-T017` Retrieval v2 Promotion Evaluation;
- `RBAW-V1-T004` externe Patch Evaluation Sidecar Harness.

Sie bindet diese Slices an eine agentische Code-Handlungslogik.

## Risiken und Folgen

| Risiko | Folge | Gegenmaßnahme |
|---|---|---|
| Überplanung | neue Dokumente ohne bessere Agentenleistung | Tasks müssen messbare Agentenwirkung oder klare Contract-Wirkung haben |
| Boundary Creep | RepoBrief wird Patchmaschine | Patch/Test/Sandbox bleiben extern; Non-Claims verpflichtend |
| Falsche Sicherheit | Impact Map wirkt vollständig | Impact Map hat Availability/Freshness/does_not_establish |
| Duplikation | Konflikt mit RBV1/RBAW | Jede Aufgabe referenziert bestehende Eigentümer |
| Zu große Tasks | langsame Umsetzung | kleine Slices, max_active_tasks=1 |

## Entscheidung

Der Plan wird registriert, aber nicht als breiter Refactor. Er ist eine Koordinations- und Contract-Initiative für Agenten-Codearbeit.

## Nächste konkrete Slices

1. Define Agent Code Task Evidence Matrix.
2. Define Agent Change Plan Contract.
3. Define Code Impact Map v1.
4. Extend Required Reading with code-change profiles.
5. Expose read-only MCP/CLI Agent Workbench surfaces.
6. Define Agent Outcome Evaluation Loop.
7. Add promotion gate for Agent Code Workbench surfaces.

## Non-claims

This plan does not implement the work, prove that RepoBrief improves agent patch quality, prove runtime correctness, prove test sufficiency, authorize merge, or prove that any currently open Lenskit PR is safe.
