# Operator ML Readiness V1

Status: active

## Ziel

Die Operator-Lernachse wird nicht durch ein neues autonomes Lernsystem ersetzt. Stattdessen wird ein kleiner evidenzgebundener Pfad aufgebaut: bestehende Grabowski-Routing-Evidenz → unabhängig geprüfter semantischer Outcome → Vibe-Lab-Experiment → optionaler Offline-ML-Vergleich.

## Systemgrenzen

- **Grabowski** bleibt Ausführungs- und Routing-Evidenzquelle. Die vorhandene hashgebundene `route_evidence` wird wiederverwendet.
- **Chronik** kann historische Evidenz referenzieren, besitzt aber keine Policy-Autorität.
- **Vibe-Lab** besitzt Experimentdesign und geprüfte Lernvorschläge, aber keine Routing- oder Runtime-Wirkung.
- **Bureau** besitzt Task- und Prioritätswahrheit.
- **Heimlern** bleibt archivierte historische Referenz und wird nicht als Runtime reaktiviert.

## Phase 1 — Shadow Capture

T001 ergänzt einen eng begrenzten Capture-Vertrag. Prozesszustände wie `completed` oder `failed` dürfen nicht als semantische Qualitätslabels umgedeutet werden. Der Datensatz braucht kanonische Route-Evidenz, ein unabhängiges semantisches Outcome oder explizite Abstention sowie Primärbelege. Rohprompts, Transkripte und private Notizen bleiben ausgeschlossen.

## Phase 2 — Readiness-Entscheidung

Das registrierte Vibe-Lab-Experiment `2026-07-23_operator-routing-ml-readiness-shadow` sammelt natürliche Fälle und entscheidet anhand der vorab festgelegten Vollständigkeits- und Privacy-Gates, ob überhaupt ein Trainingsdatensatz vorliegt. Ein PASS belegt nur Dataset-Readiness, nicht den Nutzen von ML.

## Phase 3 — Offline ML

T002 darf erst nach bestandenem Readiness-Gate starten. Erste Wahl ist eine kleine scikit-learn-basierte Vergleichsschicht mit interpretierbarer Baseline und einem Baum-/Boosting-Modell. Optuna oder MLflow werden erst ergänzt, wenn ein konkreter Experimentbedarf den Zusatzaufwand rechtfertigt. PyTorch, Online-Learning und produktive Bandit-Routen sind ausdrücklich nicht Teil von V1.

## Stop-Kriterien

- kein kanonischer Route-Beleg;
- semantischer Outcome wird aus Prozessstatus abgeleitet;
- Reviewer-/Zeit-/Repository-Leakage ist nicht beherrscht;
- Rohdatenexport privater Prompts oder Transkripte wäre nötig;
- ML-Ausgabe würde ohne separates Gate Routing, Queue, Merge, Policy oder Runtime verändern.
