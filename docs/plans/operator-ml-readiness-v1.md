# Operator ML Readiness V1

Status: active

## Aktueller Stand

Der Live-Audit vom 23. Juli 2026 zeigt zwei getrennte Wahrheiten: Die Grabowski-Task-Tabelle besitzt keine direkte kanonische Route-Spalte und keine semantische Outcome-Spalte. Gleichzeitig existiert kanonische `route_evidence` in Agent-Workspace-Manifests; im eingefrorenen Snapshot waren 27 von 46 Manifesten verifiziert. 14 dieser 27 verifizierten Route-Manifeste enthalten mindestens eine Task-Referenz, die im Task-Store matcht. Die Joinbarkeit ist damit partiell vorhanden, aber weder vollständig noch mit unabhängig geprüften semantischen Outcomes verbunden. **Folge: vorhandene Daten sind noch kein Trainingsdatensatz.** T001 schließt genau diese verbleibende Bindungs- und Outcome-Lücke.

### Was als kanonische Route-Evidenz zählt

| Quelle | Zählt als kanonisch? | Bedingung |
| --- | --- | --- |
| Validierte Grabowski Agent-Workspace `route_evidence` v1/v2 | Ja | `status=verified`, `evidence_complete=true`, deterministisch validiert |
| Späterer versionierter Nachfolger von `route_evidence` | Ja | Gleichwertige deterministische Validierung und explizite Versionsbindung |
| `argv` mit `--model`, `--model=...` oder Harness-Name | Nein | Nur diagnostischer Hinweis, keine Routing-Wahrheit |
| Task-Lifecycle `completed` / `failed` | Nein | Prozesszustand, keine semantische Qualitätsaussage |
| LLM-Kommentar oder nachträgliche Vermutung | Nein | Keine Primär- oder Routing-Autorität |

## Ziel

Die Operator-Lernachse wird nicht durch ein neues autonomes Lernsystem ersetzt. Stattdessen wird ein kleiner evidenzgebundener Pfad aufgebaut: bestehende Grabowski-Routing-Evidenz → unabhängig geprüfter semantischer Outcome → Vibe-Lab-Experiment → optionaler Offline-ML-Vergleich.

## Systemgrenzen

- **Grabowski** bleibt Ausführungs- und Routing-Evidenzquelle. Die vorhandene hashgebundene `route_evidence` wird wiederverwendet.
- **Chronik** kann historische Evidenz referenzieren, besitzt aber keine Policy-Autorität.
- **Vibe-Lab** besitzt Experimentdesign und geprüfte Lernvorschläge, aber keine Routing- oder Runtime-Wirkung.
- **Bureau** besitzt Task- und Prioritätswahrheit.
- **Heimlern** bleibt archivierte historische Referenz und wird nicht als Runtime reaktiviert.

## Phase 1 — Shadow Capture

T001 ergänzt einen eng begrenzten, versionierten Capture-Vertrag. Er bindet eine bereits validierte kanonische Route-Evidenz an eine stabile Fallidentität und danach an ein unabhängiges semantisches Outcome oder eine explizite Abstention. Prozesszustände wie `completed` oder `failed` dürfen nicht als semantische Qualitätslabels umgedeutet werden. Jede nicht-abstainende Outcome-Bewertung braucht Primärbelege. Rohprompts, Transkripte und private Notizen bleiben ausgeschlossen.

## Phase 2 — Readiness-Entscheidung

Das registrierte Vibe-Lab-Experiment `2026-07-23_operator-routing-ml-readiness-shadow` sammelt natürliche Fälle und entscheidet anhand der vorab festgelegten Vollständigkeits- und Privacy-Gates, ob überhaupt ein Trainingsdatensatz vorliegt. Ein PASS belegt nur Dataset-Readiness, nicht den Nutzen von ML.

## Phase 3 — Offline ML

T002 darf erst nach bestandenem Readiness-Gate starten. Erste Wahl ist eine kleine scikit-learn-basierte Vergleichsschicht mit interpretierbarer Baseline und einem Baum-/Boosting-Modell. Optuna oder MLflow werden erst ergänzt, wenn ein konkreter Experimentbedarf den Zusatzaufwand rechtfertigt. PyTorch, Online-Learning und produktive Bandit-Routen sind ausdrücklich nicht Teil von V1.

## Stop-Kriterien

- kein verifizierter kanonischer Route-Beleg oder keine explizite Bindung zwischen Route, Fall und Outcome;
- semantischer Outcome wird aus Prozessstatus abgeleitet;
- Reviewer-/Zeit-/Repository-Leakage ist nicht beherrscht;
- Rohdatenexport privater Prompts oder Transkripte wäre nötig;
- ML-Ausgabe würde ohne separates Gate Routing, Queue, Merge, Policy oder Runtime verändern.
