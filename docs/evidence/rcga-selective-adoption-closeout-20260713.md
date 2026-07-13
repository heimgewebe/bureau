# RepoBrief CodeGraph Selective Adoption – Closeout 2026-07-13

## Entscheidung

CodeGraph wurde nicht als separater Dienst, Index, Memory-Speicher oder Freigabeinstanz eingeführt. Übernommen wurden ausgewählte Struktur-Navigationsmechanismen als Lenskit-native, deterministische und read-only Agentenfläche.

## Bureau-Bindung

- Initiative: `REPOBRIEF-CODEGRAPH-ADOPTION-V1`
- Vertragsvorgänger: `RBAE-V1-T003`
- Implementierung: `RCGA-V1-T001` – verified
- erste Live-Kalibrierung: `RCGA-V1-T002` – verified
- Regression-Reparatur: `RCGA-V1-T003` – verified

## Implementierung

### Erster Slice

- Lenskit PR `#993`
- Merge: `456d37bd142349bc0c04925d87934eefbbc546ac`
- Ergebnis: `agent_impact_context.v1`, gerichtete Beziehungen, Testkandidaten, Verträge/Dokumentation, Einstiegspunkte, Edit Context, Kohärenz- und Integritätsgrenzen.

### Reparaturslice

- Lenskit PR `#996`
- geprüfter Head: `3bb29b0db61e7cecfe4b17778caa03575e543f9d`
- Merge: `dff582f9c4e8b5511d4ce436db81e3e245f725ec`
- vollständiger Diff SHA-256: `00d408a5482f7c35018f57152b07780752d0f36b0b04294de5f0929a621f7d5f`
- Umfang: 12 Dateien, `+1049/-62`
- Review: ein P2-Befund zu Punktsegmenten bestätigt, behoben und geschlossen.

## Reale Kalibrierung

Die erste fest gebundene Live-Messung auf Lenskit, Grabowski und Weltgewebe fand eine echte Regression: Baseline Recall `1.0`, Impact Recall `0.6666666666666666`; `tests/test_job_finalizer.py` fehlte im Grabowski-Fall. Die Fläche blieb deshalb opt-in und unbefördert.

Nach der Reparatur wurde derselbe Goldset erneut ausgeführt:

- Baseline Target Recall: `1.0`
- Impact Target Recall: `1.0`
- keine Fallregression
- kanonische Kontextpfadreduktion: `50 %`
- unabhängige strengere Bestätigung: `32 %`
- registrierte Schwelle: `20 %`
- Grabowski-Test wiedergefunden als `resolved_query`
- alle Bundles kohärent
- alle Ausgaben deterministisch
- alle Zielrepositories nach der Messung sauber
- `default_promoted=false`

Kanonischer Replay:

- Workflow `29234901905`
- Artefakt `8273035636`
- Digest `sha256:9cca7f74c3c46913f664bde5e069cb76bdac9f2e4e9cdca9d0f38ddd0945b7d4`

Unabhängige Bestätigung:

- Workflow `29234901915`
- Artefakt `8273044862`
- Digest `sha256:ab72636983fcfeffa1399d58485089fdb38cb7230d3a0219efe47f3af079a0cf`

## Übernommene Mechanismen

- eingehende und ausgehende Architekturbeziehungen mit Richtung und Evidenz;
- Testkandidaten mit getrennten Klassen `graph_edge`, `symbol_index_path_match`, `resolved_query` und `heuristic`;
- priorisierte, budgetierte Erstleseliste vor Änderungen;
- Verträge, Dokumentation und Einstiegspunkte als ergänzende Navigation;
- fail-closed Bundlekohärenz, Pfadhygiene, Lücken- und Kürzungsanzeige;
- Goldset-Messung von Recall und Kontextkompression.

## Nicht übernommen

- CodeGraph-Dienst oder persistenter CodeGraph-Index;
- CodeGraph-Memory;
- zweite Dokumentations- oder Registry-Wahrheit;
- automatische PR-Kommentare;
- Risikoscore, Reviewverdikt oder Mergefreigabe;
- Coverage-, Call-Graph- oder Blast-Radius-Vollständigkeitsbehauptung.

## Abschlussurteil

Die selektive Übernahme trägt auf dem festen Drei-Repository-Goldset: gleicher Recall bei deutlich weniger Kontext. Das belegt einen begrenzten Navigationsnutzen, keine allgemeine Agentenverbesserung. Die Fläche bleibt opt-in. Eine Standardaktivierung erfordert einen neuen Bureau-Ball und einen breiteren Agenten-Benchmark.
