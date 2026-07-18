# Weltgewebe Semantic Search v1

## Entscheidung

Die nützliche Semantik-Schicht wird direkt in `heimgewebe/weltgewebe` integriert. SemantAH wird nicht als eigener Dienst weiterentwickelt. PostgreSQL bleibt die einzige persistente Wahrheit; Suchrepräsentationen und Embeddings sind regenerierbare Projektionen.

Die Initiative liefert keine allgemeine Semantikplattform. Sie verbessert ausschließlich die sichtbarkeitsgebundene Weltgewebe-Suche und eine klar getrennte Ansicht „Ähnliche Knoten“.

## Alternative Sinnachse

Würde maximale kurzfristige Liefergeschwindigkeit höher gewichtet als Wahrheitsgrenzen und Datenschutz, wäre ein externer Embedding-Dienst mit separatem Index schneller aufzusetzen. Dieser Pfad wird verworfen: Er schafft eine zweite Zustandswahrheit, neue Kosten- und Verfügbarkeitsabhängigkeiten sowie zusätzliche Lösch- und Sichtbarkeitsrisiken.

Würde ausschließlich lokale Souveränität gewichtet, könnte ein großes lokales Modell unabhängig von gemessener Relevanz gewählt werden. Auch das wird verworfen: Das kleinste lokale mehrsprachige Modell gewinnt, sofern es die Qualitätsgrenzen erfüllt. `qwen3-embedding:8b` dient nur als lokaler Qualitätsmaßstab. Ein kostenloser OpenRouter-Endpunkt ist höchstens ein begrenzter Gegenkandidat mit synthetischen oder nicht sensiblen Daten und ohne Produktionsabhängigkeit oder angenommenen API-Schlüssel.

## Wahrheits- und Datenvertrag

- PostgreSQL ist kanonisch.
- Die Suchprojektion ist vollständig regenerierbar.
- Knotenänderungen dürfen nicht vom Embedding-Provider abhängen; Provider-Ausfall markiert die Projektion als nachzuholen.
- Eine Projektion darf nur die aktuelle `source_revision` eines Knotens schreiben. Veraltete Worker-Ergebnisse werden verworfen.
- Modellwechsel erzeugen eine neue vollständige Indexgeneration. Modell-ID, Modellrevision, Dimension und Generation sind Bestandteil der Identität; Vektoren verschiedener Generationen werden nie gemeinsam abgefragt.
- Lexikalische Suche bleibt als technischer Notfall-Fallback erhalten, nicht als parallele Produktwahrheit.

Vorgesehene Projektionsfelder sind `node_id`, `source_revision`, `content_sha256`, `title`, `tags`, `searchable_text`, lexikalische Suchrepräsentation, Embedding, Provider, Modell-ID, Modellrevision, Dimension, Indexgeneration, Sichtbarkeitsklasse, Indexierungsstatus und `indexed_at`.

## Indexierbare und ausgeschlossene Inhalte

Indexierbar sind nur tatsächlich suchbare Knotenfelder: Titel, Kurzbeschreibung, ausführlicher Informationstext, Tags, Knotenart, Sprache, öffentlich freigegebener Ortsname und zulässige Handlungsbegriffe.

Nicht eingebettet oder anderweitig in die Suchprojektion übernommen werden E-Mail-Adressen, Sitzungen, Authentifizierungsdaten, interne Moderationsdaten, private Gespräche, verborgene Orte, nicht sichtbare historische Versionen, technische Auditdaten und personenbezogene Felder ohne Suchzweck.

Sichtbarkeit und Löschstatus werden vor der Kandidatenauswahl angewendet. Nachträgliches Herausfiltern bereits gerankter verborgener Treffer ist unzulässig.

## Retrieval- und Rankingvertrag

Reine Vektorsuche ist verboten. Der Server kombiniert testbar und mit stabilen Tie-Breaks:

1. exakten Titel,
2. exaktes Tag,
3. Titelpräfix,
4. Schreibfehlertoleranz über Trigramme,
5. PostgreSQL-Volltext,
6. semantische Ähnlichkeit,
7. stabile Tie-Breaks.

Bestehende Filter bleiben erhalten. Semantische Ähnlichkeit darf keinen echten Faden, keine kuratierte Beziehung und keine gemeinsame Autorenschaft vortäuschen. „Ähnliche Knoten“ ist eine getrennte, ausdrücklich maschinell berechnete Oberfläche.

## Modellwahl

T002 vergleicht die bestehende lexikalische Suche, PostgreSQL-Volltext und Trigramme, ein kompaktes lokales mehrsprachiges Modell sowie lokales `qwen3-embedding:8b` als Qualitätsmaßstab. OpenRouter-Free ist nur ein optionaler, begrenzter Gegenkandidat mit synthetischen oder nicht sensiblen Daten, ohne Produktionsabhängigkeit und ohne Annahme eines vorhandenen Schlüssels. Das kleinste lokale Modell gewinnt, sofern es die Qualitätsgrenzen erfüllt.

## Qualitätsgates

Vor T008 müssen mindestens belegt sein:

- Exakte Titel- und Tagtreffer bleiben auf Rang 1.
- Mindestens 85 Prozent der relevanten natürlichen Goldset-Anfragen liefern einen relevanten Top-3-Treffer.
- Falsche Top-1-Treffer nehmen gegenüber der lexikalischen Basis nicht zu.
- Gelöschte oder unsichtbare Inhalte erscheinen nie.
- Embedding-Ausfall blockiert keine Knotenänderung.
- Veraltete Worker überschreiben keine neueren Revisionen.
- Vollständiger Indexneuaufbau ist reproduzierbar.
- Modellwechsel ist generationsgebunden.
- Multi-Instance-Betrieb bleibt konsistent.
- Backup, Restore und PITR umfassen die Suchprojektion oder belegen deren vollständige Regeneration.
- Keine unbeschränkten API-Kosten entstehen.
- ANN oder HNSW wird erst bei einem gemessenen Skalierungsengpass erwogen.

Das Goldset erhält pro Fall mindestens: stabile Fall-ID, Sprache, Anfrage, sichtbaren Suchkontext, relevante Knoten-IDs, erwartete Rangklasse, ausgeschlossene Knoten-IDs und Begründung. Reale personenbezogene Daten sind ausgeschlossen; produktionsnahe Beispiele werden redigiert oder synthetisch erzeugt.

## Hard Cut und Nichtziele

Nicht Bestandteil der Zielarchitektur sind:

- eine separate `apps/semantic-service`-Runtime,
- JSONL-Persistenz,
- ein eigenständiger `indexd`-Dienst,
- dauerhafte Git- oder Cargo-Abhängigkeit auf SemantAH,
- allgemeine Namespace-Plattformabstraktionen,
- Obsidian-Pipeline, Wissensgraph, Related-Blöcke, Knowledge Observatory oder Daily Insights,
- HausKI- oder RepoBrief-Rollen,
- Commonworld-Anbindung,
- Shadow-Modus,
- automatische Fäden, Beziehungen, Löschungen oder Zusammenführungen aus Ähnlichkeit,
- ANN-/HNSW-Arbeit ohne gemessenen Weltgewebe-Bedarf.

Brauchbare SemantAH-Konzepte wie Providergrenzen, Dimensionsprüfung, Normalisierung, deterministische Cosinus-Referenzsuche, Benchmarks und Tests werden zielgerichtet neu implementiert und an Weltgewebes Verträge angepasst. SemantAH-Code wird nicht als dauerhafte Abhängigkeit übernommen.

## Taskkette

1. **T001 – Architekturvertrag, Wahrheitsgrenzen und Hard Cut:** reale Codeorganisation prüfen; diesen Vertrag und die Goldset-Grundlage im Weltgewebe veröffentlichen.
2. **T002 – Relevanz-Goldset und Embedding-Modellwahl:** lexikalische Basis, FTS/Trigramme, lokales Modell, `qwen3-embedding:8b` und optional OpenRouter-Free vergleichen.
3. **T003 – PostgreSQL-Suchgrundlage und pgvector-Fähigkeit:** Erweiterungsfähigkeit, Schema, Migration, Backup/Restore/PITR und Betriebsbild belegen.
4. **T004 – interner Embedding- und Ranking-Kern:** Providergrenze, Normalisierung, Dimensions- und Generationsprüfung sowie hybrides Ranking implementieren.
5. **T005 – idempotente Projektion, Worker, Backfill und Löschfortpflanzung:** revisionssichere Multi-Instance-Verarbeitung und reproduzierbaren Neuaufbau liefern.
6. **T006 – hybride serverseitige Such-API:** Sichtbarkeit vor Retrieval, bestehende Filter und technischen lexikalischen Fallback liefern.
7. **T007 – Websuche und „Ähnliche Knoten“:** Suchoberfläche integrieren und maschinelle Ähnlichkeit klar von Beziehungen trennen.
8. **T008 – vollständige Abnahme, direkter Rollout und öffentlicher Live-Beweis:** grüne CI, direkter Rollout, öffentlicher Readback und Betriebsbelege.
9. **T009 – SemantAH stilllegen und bereinigen:** erst nach T008 Runtime-Rollen entfernen, Repository archivieren und Bureau/Systemkatalog nachziehen.

Nach öffentlichem T008-Beweis supersedet oder schließt T009 `SEMANTAH-USEFULNESS-V1`, `SEMANTAH-INDEXD-SCALING-V1` und `SEMANTAH-E2E-PORTABILITY-V1`. Vorher bleiben sie unverändert.

## Umsetzungsschnitt T001

T001 verändert noch keine Datenbank, Such-API, Produktionskonfiguration oder SemantAH-Runtime. Der Weltgewebe-PR enthält nach Prüfung der realen Struktur nur den kanonischen Architekturvertrag, eine maschinenlesbare Goldset-Grundlage mit Validierung, explizite Nichtziele und Taskgrenzen. Vor Merge sind ein vollständiges externes Diff-Artefakt, ein an exakten Head und Diff-SHA256 gebundener Self-Review und grüne CI Pflicht.

## Aktuelle Betriebsabhängigkeit

Am 18. Juli 2026 um 16:36:40 Uhr MESZ wurde auf `wg-prod-1` ein fehlgeschlagener Produktions-Reconciler, inaktives Caddy und kein Listener auf 80/443 beobachtet. Das ist kein T001-Implementierungsfehler und wird hier nicht repariert. Der Zustand muss vor T008 neu gelesen und gegebenenfalls über einen bereits bestehenden oder separat registrierten Betriebsauftrag behoben werden.

## Stopbedingungen

Die Initiative wird gestoppt oder neu geschnitten, wenn Sichtbarkeit nicht vor Retrieval durchsetzbar ist, PostgreSQL keine tragfähige Betriebsoption für die Projektion bietet, der lokale Modellpfad die Qualitätsgrenzen klar verfehlt und keine ausdrückliche Kostenfreigabe besteht, oder der erwartete Relevanzgewinn gegenüber FTS/Trigrammen praktisch unbedeutend bleibt.
