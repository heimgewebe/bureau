# Optionale Diff-Auslieferung v1

Stand: 30. Juli 2026

## Normative Entscheidung

Ein benutzerseitig bereitgestelltes, kopierbares oder herunterladbares Diff-Artefakt ist keine Voraussetzung für Review, Merge oder Verifikation.

Vor einem nichttrivialen Merge bleiben erforderlich:

- exakte Bindung an Repository, Base und aktuellen Head;
- vollständiger intern ermittelter Diff und dessen SHA-256;
- head- und diffgebundener Review ohne offene materielle Befunde;
- grüne erforderliche CI auf demselben Head;
- frischer Merge-, Lease- und Plattformzustand unmittelbar vor dem Effekt;
- autoritativer Post-Merge-Readback.

## Optionale Nutzung

Ein Diff-Artefakt darf weiterhin erzeugt und ausgeliefert werden, wenn der Nutzer es ausdrücklich verlangt oder ein begründetes Sonderrisiko eine externe Übergabe sinnvoll macht. Eine solche Auslieferung ist zusätzliche Evidenz und erteilt keine Merge-, Review- oder Deploymentautorität.

## Historische Evidenz

Bereits terminale Aufgaben, Receipts und Abschlussberichte werden nicht rückwirkend umgeschrieben. Ihre damaligen Delivery-Aussagen bleiben historische Wahrheit, entfalten aber keine aktuelle normative Sperrwirkung.
