# RepoBrief Evidence Call Graph v1

## Zweck

RepoBrief erweitert die vorhandene Definitionensuche um einen evidenzgestuften Symbol-Relationsgraphen. Der erste produktive Slice bleibt Python-spezifisch, statisch, deterministisch und read-only. Er beantwortet, wo ein Symbol aufgerufen wird, welche belegten Aufrufer existieren und welche Aufrufe ein Symbol selbst enthält.

## Phasen

1. **V1-Vertrag und konservativer Python-Producer (`RPU-V1-T023`).** S1 bedeutet eindeutig statisch aufgelöst; S0 bedeutet Kandidat, Mehrdeutigkeit oder nicht aufgelöste dynamische Beziehung.
2. **Goldset und Promotionsgate (`RPU-V1-T024`).** Präzision, Recall, False Positives, Speicher, Laufzeit und Agentennutzen werden getrennt gemessen.
3. **Edit Context (`RPU-V1-T025`).** Nur nach bestandenem Gate werden Caller/Callee-Belege in priorisierte Änderungskontexte aufgenommen.
4. **S2-Laufzeitoverlay (`RPU-V1-T026`).** Beobachtete Aufrufe werden laufgebunden ergänzt und niemals als Vollständigkeitsbeweis gelesen.
5. **Mehrsprachen- und Systemrelationen (`RPU-V1-T027`/`T028`).** SCIP sowie Build-, Test-, Schema- und Artefaktbeziehungen bleiben getrennte Adapter.

## Architekturgrenze

Der Graph ist Navigationsevidenz, keine zweite Inhaltswahrheit. `canonical_md` bleibt Inhaltswahrheit. RepoBrief führt keinen Zielcode aus, mutiert kein Git, erzeugt keinen Patch, bewertet keine Testhinlänglichkeit und erteilt keine Mergefreigabe.

## Promotionsregel

Ein eindrucksvoller Graph reicht nicht. Eine Standardroute ist erst zulässig, wenn ein fixes Goldset mindestens 0,97 S1-Präzision, keinen Fallrückschritt und mindestens 40 Prozent weniger Kontextpfade bei gleichem oder besserem Ziel-Recall belegt.

## Nachlauf aus der PR-1018-Review

6. **Skalierte Navigation (`RPU-V1-T029`).** Persistierte Pre-Aggregation wird gegen einen beim Laden erzeugten In-Memory-Index und den bisherigen linearen Scan gemessen. Entscheidend sind Ergebnisgleichheit, Bundle-Größe, Speicher und wiederholte MCP-Latenz.
7. **Producer- und Vertragszerlegung (`RPU-V1-T030`).** Scope-Erfassung, Call-Aufzeichnung, Auflösung und Validierung werden ohne Semantikdrift getrennt; generative AST-Tests falsifizieren Range- und Scope-Annahmen.
8. **Inkrementelle und parallele Erzeugung (`RPU-V1-T031`).** Datei-Reuse und begrenzte Parallelität sind nur zulässig, wenn sie bytegleich zu einem sauberen Vollaufbau bleiben und korrekt invalidieren.
9. **Kontrollierte S1-Rekall-Erweiterung (`RPU-V1-T032`).** Vererbung, Mixins, `super()`, Receiver-Aliase und Import-Sonderfälle werden reason-spezifisch gemessen. Ohne bestandenes 0,97-Präzisionsgate bleiben sie S0.
10. **Formatierungsbaseline (`RPU-V1-T033`).** Der mit Ruff 0.15.13 festgestellte Formatdrift in den vier T029-Navigationsdateien wird als isolierter, semantikfreier Hygiene-Patch bereinigt; Umfang und künftiger Format-Gatevertrag werden ausdrücklich festgelegt.

Diese Phasen sind keine Nachbesserungsbehauptung für den bereits gemergten V1-Graphen. Sie trennen Skalierung, Wartbarkeit und höheren Recall von den konservativen Beweisgrenzen des bestehenden Artefakts.
