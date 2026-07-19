# Weltgewebe Semantic Search v1

## Entscheidung

Die nützliche Semantik-Schicht wird direkt in `heimgewebe/weltgewebe` integriert. SemantAH wird nicht als eigener Dienst weiterentwickelt. PostgreSQL bleibt die einzige persistente Wahrheit; Suchrepräsentationen und Embeddings sind regenerierbare Projektionen.

Die Initiative liefert keine allgemeine Semantikplattform. Sie verbessert ausschließlich die sichtbarkeitsgebundene Weltgewebe-Suche und eine klar getrennte Ansicht „Ähnliche Knoten“.

## Alternative Sinnachse

Würde maximale kurzfristige Liefergeschwindigkeit höher gewichtet als Wahrheitsgrenzen und Datenschutz, wäre ein externer Embedding-Dienst mit separatem Index schneller aufzusetzen. Dieser Pfad wird verworfen: Er schafft eine zweite Zustandswahrheit, neue Kosten- und Verfügbarkeitsabhängigkeiten sowie zusätzliche Lösch- und Sichtbarkeitsrisiken.

Würde ausschließlich lokale Souveränität gewichtet, könnte ein großes lokales Modell unabhängig von gemessener Relevanz gewählt werden. Auch das wird verworfen: Das kleinste lokale mehrsprachige Modell gewinnt, sofern es die Qualitätsgrenzen erfüllt. Der T002-Benchmark identifizierte `qwen3-embedding:8b` als einzigen getesteten Kandidaten, der alle harten T002-Gates erfüllte. Das macht ihn zum lokalen Referenzkandidaten für T003/T004, nicht zum Produktionsmodell. Ein kostenloser OpenRouter-Endpunkt bleibt höchstens ein begrenzter Gegenkandidat mit synthetischen oder nicht sensiblen Daten und ohne Produktionsabhängigkeit oder angenommenen API-Schlüssel.

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

## T002-Modell- und Baselinebeleg

Der gemergte synthetische Benchmark verglich auf identischem Goldset:

| Pfad | natürliche Top-3 | falsche Top-1 | Lecks |
| --- | ---: | ---: | ---: |
| bestehende Client-Teilstringsuche | 0/19 | 0 | 0 |
| FTS-/Trigramm-Referenz | 11/19 | 1 | 0 |
| `qwen3-embedding:0.6b` | 18/19 | 3 | 0 |
| `qwen3-embedding:4b` | 19/19 | 2 | 0 |
| `qwen3-embedding:8b` | 19/19 | 1 | 0 |

Nur `qwen3-embedding:8b` erfüllte alle harten T002-Gates. Der Befund gilt ausschließlich für den lokalen synthetischen Harness. Er belegt weder Produktionslatenz noch PostgreSQL-Parität, pgvector-Fähigkeit, Betriebsstabilität oder eine Produktionsfreigabe des Modells.

T002 bindet Dataset, Schema, Benchmarkquelle, Goldset-Validator, Modellidentität und Ergebnisaggregate per SHA-256. Der Harness verweigert Modellwechsel während eines Laufs, nichtendliche oder dimensionsinkonsistente Vektoren, doppelte JSON-Schlüssel, unbekannte Ergebnisfelder, übergroße Antworten sowie PII- oder nichtsynthetisch markierte Fixtures. Rohvektoren und Providerrohdaten werden nicht persistiert.

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

1. **T001 – Architekturvertrag, Wahrheitsgrenzen und Hard Cut – verifiziert:** Vertrag und Goldset-Grundlage wurden über Weltgewebe-PR #1485 veröffentlicht.
2. **T002 – Relevanz-Goldset und Embedding-Modellwahl – verifiziert:** Weltgewebe-PR #1495 veröffentlichte den synthetischen Harness, Integritätsbindungen und lokalen Modellvergleich; `qwen3-embedding:8b` ist nur Referenzkandidat.
3. **T003 – PostgreSQL-Suchgrundlage und pgvector-Fähigkeit – aktuell:** Erweiterungsfähigkeit, Schema, Migration, FTS-/Trigramm-Parität, Backup/Restore/PITR und Multi-Instance-Grenzen belegen, ohne Runtime- oder Produktionseffekt.
4. **T004 – interner Embedding- und Ranking-Kern:** Providergrenze, Normalisierung, Dimensions- und Generationsprüfung sowie hybrides Ranking implementieren.
5. **T005 – idempotente Projektion, Worker, Backfill und Löschfortpflanzung:** revisionssichere Multi-Instance-Verarbeitung und reproduzierbaren Neuaufbau liefern.
6. **T006 – hybride serverseitige Such-API:** Sichtbarkeit vor Retrieval, bestehende Filter und technischen lexikalischen Fallback liefern.
7. **T007 – Websuche und „Ähnliche Knoten“:** Suchoberfläche integrieren und maschinelle Ähnlichkeit klar von Beziehungen trennen.
8. **T008 – vollständige Abnahme, direkter Rollout und öffentlicher Live-Beweis:** grüne CI, direkter Rollout, öffentlicher Readback und Betriebsbelege.
9. **T009 – SemantAH stilllegen und bereinigen:** erst nach T008 Runtime-Rollen entfernen, Repository archivieren und Bureau/Systemkatalog nachziehen.

Nach öffentlichem T008-Beweis supersedet oder schließt T009 `SEMANTAH-USEFULNESS-V1`, `SEMANTAH-INDEXD-SCALING-V1` und `SEMANTAH-E2E-PORTABILITY-V1`. Vorher bleiben Repository, Initiativen und aktive SemantAH-Rollen unverändert.

## Fortschritt

- **T001 ist verifiziert:** Weltgewebe-PR #1485 wurde als Merge-Commit `f00afacc7be4cc551c81c5511faf5f817b04f700` nach grüner CI und zweiachsigem R2-Review gemergt. Der vollständige GitHub-Diff ist an SHA-256 `745483199f2b955a8ac37521445a85f8b9543e92ecf3ff69ed99b3a21ae7554f` gebunden.
- **T002 ist verifiziert:** Weltgewebe-PR #1495 wurde mit attestiertem Head `31ca3c433dcaf3b941c5e1c95167a68e9f68ceb8` und Merge-Commit `adc060cfbb9d055a7b63c494fa042e7c57ca7bea` gemergt. Der kanonische GitHub-Diff ist an SHA-256 `b54ec09ce52fe7e109b18da8f4ed7e5fc5e33783ff75252dd354c605ec6988e7` gebunden; der lokale Binärdiff an `6ffdc08f17d68ecb72a0a4dfe8ade167ab47dabbe6b0cf7a1a35410e4e2e1375`.
- **T003 ist der aktuelle Task:** reale PostgreSQL-, Extension-, Schema-, Paritäts-, Backup-/Restore-/PITR- und Regenerationsfähigkeit belegen, bevor Embedding- oder Ranking-Runtime entsteht.
- **SemantAH bleibt unverändert:** keine Archivierung, keine Runtime-Entfernung und keine Bereinigung vor T008/T009.

## T002-Test- und Reviewbindung

Auf dem veröffentlichten T002-Head bestanden 816 Docmeta-, 191 Agent- und 277 CI-Prüfungen, 27 fokussierte Semantic-Search-Tests sowie Generator-, Struktur-, Shell-, Plattform- und Vertragsgates; 11 Skips waren erwartet. Zwei getrennte R2-Self-Reviews auf den Achsen Correctness und Data Integrity wurden vom repository-eigenen Review-Evidence-Gate akzeptiert. Required Merge Gate, Review Evidence Gate, Web E2E, CodeQL, Docs Guard, PostgreSQL Integration Proofs, Cloudflare Pages und Vercel waren vor Merge grün.

T002 implementiert keine PostgreSQL-Migration, Search-API, Worker-, Web- oder Deploymentfunktion. Der Merge veröffentlicht Architektur-, Benchmark- und Testbelege, aber keine produktive semantische Suche.

## Aktuelle öffentliche Produktionsprüfung

Der erste T002-Readback unmittelbar nach Merge beobachtete noch den vorangehenden Live-Commit `d9a5377a07e4e9728778f327fa87668945d007cf`. Beim erneuten Bureau-Closeout-Readback am 19. Juli 2026 bestanden DNS für Root, WWW und API, HTTP→HTTPS, beide HTTPS-Roots, Map-Route, API-Readiness, Version-JSON, lokalen Basemap-Stil, Glyphen sowie stabile und versionierte Hamburg- und Schleswig-Holstein-PMTiles. Der dabei öffentlich gelesene Live-Commit war `b5a9383fc36b381bf5a68fd2e9a287d13f2caa82`; Git bestätigt den T002-Merge-Commit `adc060cfbb9d055a7b63c494fa042e7c57ca7bea` als dessen Vorfahren.

Das belegt, dass der T002-Quellstand inzwischen in der ausgelieferten Commitlinie enthalten ist und die bestehende öffentliche Oberfläche gesund ist. Es belegt weiterhin keine produktive semantische Suche: PR #1495 besitzt keinen Runtime- oder Deployschnitt. Ein semantischer öffentlicher Live-Beweis bleibt T008 vorbehalten.

## Stopbedingungen

Die Initiative wird gestoppt oder neu geschnitten, wenn Sichtbarkeit nicht vor Retrieval durchsetzbar ist, PostgreSQL keine tragfähige Betriebsoption für die Projektion bietet, der lokale Modellpfad die Qualitätsgrenzen klar verfehlt und keine ausdrückliche Kostenfreigabe besteht, oder der erwartete Relevanzgewinn gegenüber FTS/Trigrammen praktisch unbedeutend bleibt.
