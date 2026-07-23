# Weltgewebe Semantic Search v1

## Entscheidung

Die nützliche Semantik-Schicht wird direkt in `heimgewebe/weltgewebe` integriert. SemantAH wird nicht als eigener Dienst weiterentwickelt. PostgreSQL bleibt die einzige persistente Wahrheit; Suchrepräsentationen und Embeddings sind regenerierbare Projektionen.

Die Initiative liefert keine allgemeine Semantikplattform. Sie verbessert ausschließlich die sichtbarkeitsgebundene Weltgewebe-Suche und eine klar getrennte Ansicht „Ähnliche Knoten“.

## Alternative Sinnachse

Würde maximale kurzfristige Liefergeschwindigkeit höher gewichtet als Wahrheitsgrenzen und Datenschutz, wäre ein externer Embedding-Dienst mit separatem Index schneller aufzusetzen. Dieser Pfad wird verworfen: Er schafft eine zweite Zustandswahrheit, neue Kosten- und Verfügbarkeitsabhängigkeiten sowie zusätzliche Lösch- und Sichtbarkeitsrisiken.

Würde ausschließlich lokale Souveränität gewichtet, könnte ein großes lokales Modell unabhängig von gemessener Relevanz gewählt werden. Auch das wird verworfen: Das kleinste lokale mehrsprachige Modell gewinnt, sofern es die Qualitätsgrenzen erfüllt. T002 identifizierte `qwen3-embedding:8b` als vorsichtigen Referenzkandidaten. Der reale T003-plus-T004-Fusionsbeleg zeigt nun, dass `qwen3-embedding:4b` als kleinstes getestetes lokales Modell alle T004-Gates erfüllt. `qwen3-embedding:8b` bleibt die stärkere Vergleichsobergrenze. Keines der Modelle ist damit für Produktion freigegeben. Ein kostenloser OpenRouter-Endpunkt bleibt höchstens ein begrenzter Gegenkandidat mit synthetischen oder nicht sensiblen Daten und ohne Produktionsabhängigkeit oder angenommenen API-Schlüssel.

## Wahrheits- und Datenvertrag

- PostgreSQL ist kanonisch.
- Die Suchprojektion ist vollständig regenerierbar.
- Knotenänderungen dürfen nicht vom Embedding-Provider abhängen; Provider-Ausfall markiert die Projektion als nachzuholen.
- Eine Projektion darf nur die aktuelle `source_revision` eines Knotens schreiben. Veraltete Worker-Ergebnisse werden verworfen.
- Modellwechsel erzeugen eine neue vollständige Indexgeneration. Modell-ID, Modellrevision, Dimension und Generation sind Bestandteil der Identität; Vektoren verschiedener Generationen werden nie gemeinsam abgefragt.
- Die autoritative, sichtbarkeitsgefilterte T003-PostgreSQL-Reihenfolge bleibt maßgeblich. T004 darf höchstens einen ausreichend sicheren semantischen Zusatzkandidaten anhängen.
- Semantische Gleichstände werden deterministisch nach aufsteigender Knoten-ID entschieden.
- Lexikalischer Fallback ist nur bei nachgewiesener Provider-Nichtverfügbarkeit zulässig; Identitäts-, Datenschutz-, Dimensions- und Generationsfehler bleiben fail-closed.

Vorgesehene Projektionsfelder sind `node_id`, `source_revision`, `content_sha256`, `title`, `tags`, `searchable_text`, lexikalische Suchrepräsentation, Embedding, Provider, Modell-ID, Modellrevision, Dimension, Indexgeneration, Sichtbarkeitsklasse, Indexierungsstatus und `indexed_at`.

## Indexierbare und ausgeschlossene Inhalte

Indexierbar sind nur tatsächlich suchbare Knotenfelder: Titel, Kurzbeschreibung, ausführlicher Informationstext, Tags, Knotenart, Sprache, öffentlich freigegebener Ortsname und zulässige Handlungsbegriffe.

Nicht eingebettet oder anderweitig in die Suchprojektion übernommen werden E-Mail-Adressen, Sitzungen, Authentifizierungsdaten, interne Moderationsdaten, private Gespräche, verborgene Orte, nicht sichtbare historische Versionen, technische Auditdaten und personenbezogene Felder ohne Suchzweck.

Sichtbarkeit und Löschstatus werden vor der Kandidatenauswahl angewendet. Nachträgliches Herausfiltern bereits gerankter verborgener Treffer ist unzulässig.

## Retrieval- und Rankingvertrag

Reine Vektorsuche ist verboten. Der Server kombiniert testbar und mit stabilen Tie-Breaks:

1. autoritative PostgreSQL-Reihenfolge aus exaktem Titel, exaktem Tag, Titelpräfix, Trigrammen und Volltext,
2. höchstens einen ausreichend sicheren semantischen Zusatzkandidaten,
3. aufsteigende Knoten-ID als semantischen Tie-Break.

Bestehende Filter bleiben erhalten. Semantische Ähnlichkeit darf keinen echten Faden, keine kuratierte Beziehung und keine gemeinsame Autorenschaft vortäuschen. „Ähnliche Knoten“ ist eine getrennte, ausdrücklich maschinell berechnete Oberfläche.

## T002-Modell- und Baselinebeleg

Der gemergte synthetische T002-Benchmark verglich auf identischem Goldset:

| Pfad | natürliche Top-3 | falsche Top-1 | Lecks |
| --- | ---: | ---: | ---: |
| bestehende Client-Teilstringsuche | 0/19 | 0 | 0 |
| FTS-/Trigramm-Referenz | 11/19 | 1 | 0 |
| `qwen3-embedding:0.6b` | 18/19 | 3 | 0 |
| `qwen3-embedding:4b` | 19/19 | 2 | 0 |
| `qwen3-embedding:8b` | 19/19 | 1 | 0 |

Nur `qwen3-embedding:8b` erfüllte damals alle harten T002-Gates. Der Befund galt ausschließlich für den lokalen synthetischen Harness und belegte weder Produktionslatenz noch PostgreSQL-Parität, pgvector-Fähigkeit, Betriebsstabilität oder eine Produktionsfreigabe.

T002 bindet Dataset, Schema, Benchmarkquelle, Goldset-Validator, Modellidentität und Ergebnisaggregate per SHA-256. Der Harness verweigert Modellwechsel während eines Laufs, nichtendliche oder dimensionsinkonsistente Vektoren, doppelte JSON-Schlüssel, unbekannte Ergebnisfelder, übergroße Antworten sowie PII- oder nichtsynthetisch markierte Fixtures. Rohvektoren und Providerrohdaten werden nicht persistiert.

## T004-Ranking- und Qualitätsbeleg

Weltgewebe-PR #1502 implementierte den internen, ausführbaren Referenzkern. PR #1506 härtete die Beweisgrenzen: Die T003-PostgreSQL-Reihenfolge bleibt autoritativ, semantisch wird höchstens ein sicherer Kandidat ergänzt, Gleichstände werden nach Knoten-ID entschieden und nur `ProviderUnavailableError` aktiviert den lexikalischen Fallback. Die Rankingrevision lautet `weltgewebe-hybrid-ranking-v2`.

Der Receipt-Checker rekonstruiert die Aggregate aus 24 vollständigen Fallrangfolgen, davon 19 natürliche Fälle:

| Pfad | natürliche Top-3 | falsche Top-1 | Sichtbarkeitslecks | Entscheidung |
| --- | ---: | ---: | ---: | --- |
| T003 PostgreSQL | 14/19 | 0 | 0 | autoritative lexikalische Basis |
| `qwen3-embedding:0.6b` + T003 | 14/19 | 0 | 0 | kein Zusatznutzen |
| `qwen3-embedding:4b` + T003 | 18/19 | 0 | 0 | kleinster qualifizierter lokaler Kandidat |
| `qwen3-embedding:8b` + T003 | 19/19 | 0 | 0 | stärkere Vergleichsobergrenze |

`natural_top3_relevant_count` bezieht sich nur auf die 19 natürlichen Fälle. Die Aggregate über alle 24 Fälle und die erwartete Rangklasse `semantic` besitzen deshalb andere, aber konsistente Zähler. Der Checker lehnt jede Abweichung ab. Rohvektoren und Providerrohdaten werden nicht persistiert.

Der primäre Binärdiff von PR #1502 ist an SHA-256 `b4fb532d9a10f84ea41c4d0603accbb3a3bd471a673f3859913f76aa141a2c4c` gebunden. Der Hardening-Binärdiff von PR #1506 ist an `ef9d37a139cbfbce837e4904a2c11f1ad05a37d519ed7027f9a760d9181af655` gebunden. Der aktuelle Weltgewebe-Main `de8ec3da449d8fff2f0495e9c685eebb3c0dc061` enthält für alle sechs Hardening-Dateien dieselben Git-Blobs wie der geprüfte Head `fc710aa4c0d0819d4acaef5b0034f8d88a9eb6b2`.

T004 ist damit verifiziert, aber keine Produktionsfreigabe. Es existieren weiterhin keine persistente Projektion, kein Worker, kein Backfill, keine Search-API, keine Webintegration, kein Produktionsranking, kein Deployment und keine SemantAH-Stilllegung.

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

## T005-Projektions- und Workergrenzen

T005 ist die verifizierte Projektions- und Workerstufe. Sie implementiert ausschließlich:

- eine regenerierbare PostgreSQL-Suchprojektion,
- revisions- und generationsgebundene idempotente Worker-Verarbeitung,
- fail-closedes Verwerfen veralteter Worker-Ergebnisse,
- Multi-Instance-sichere Konflikt-, Sperr- und Retry-Grenzen,
- begrenzbaren Backfill und vollständigen Rebuild,
- Lösch-, Sichtbarkeits- und Revisionsfortpflanzung,
- einen nachholbaren Zustand bei Provider-Ausfall,
- Betriebsbelege ohne Rohvektoren oder sensible Logdaten.

T005 implementiert ausdrücklich keine Search-API, keine Websuche, keine Oberfläche „Ähnliche Knoten“, keinen Produktionsrollout, keinen öffentlichen Live-Beweis und keine SemantAH-Stilllegung. ANN oder HNSW bleibt ohne gemessenen Bedarf ausgeschlossen.

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
2. **T002 – Relevanz-Goldset und Embedding-Modellwahl – verifiziert:** Weltgewebe-PR #1495 veröffentlichte den synthetischen Harness, Integritätsbindungen und lokalen Modellvergleich; `qwen3-embedding:8b` blieb zunächst Referenzkandidat.
3. **T003 – PostgreSQL-Suchgrundlage und pgvector-Fähigkeit – verifiziert:** PR #1500 belegt PostgreSQL 16.14, pg_trgm 1.6, Projektion, Regeneration, Backup/Restore und Konfliktgrenzen; pgvector bleibt mangels Verfügbarkeit gestoppt.
4. **T004 – interner Embedding- und Ranking-Kern – verifiziert:** PR #1502 und Hardening-PR #1506 liefern Provider-, Normalisierungs-, Dimensions-, Generations- und Hybrid-Ranking-Beleg; 4B ist kleinster qualifizierter lokaler Kandidat, 8B Vergleichsobergrenze.
5. **T005 – idempotente Projektion, Worker, Backfill und Löschfortpflanzung – verifiziert:** PR #1529 liefert revisionssichere Multi-Instance-Verarbeitung, lokalen Backfill und reproduzierbaren Neuaufbau.
6. **T006 – hybride serverseitige Such-API – verifiziert:** PR #1546 bindet autoritative Sichtbarkeit und Autorisierung vor Retrieval, wahrt das lexikalische PostgreSQL-Ranking und ergänzt Semantik nur nach dem T004-Vertrag.
7. **T007 – Websuche und „Ähnliche Knoten“ – aktuell:** Suchoberfläche integrieren und maschinelle Ähnlichkeit klar von Beziehungen trennen.
8. **T008 – vollständige Abnahme, direkter Rollout und öffentlicher Live-Beweis:** grüne CI, direkter Rollout, öffentlicher Readback und Betriebsbelege.
9. **T009 – SemantAH stilllegen und bereinigen:** erst nach T008 Runtime-Rollen entfernen, Repository archivieren und Bureau/Systemkatalog nachziehen.

Nach öffentlichem T008-Beweis supersedet oder schließt T009 `SEMANTAH-USEFULNESS-V1`, `SEMANTAH-INDEXD-SCALING-V1` und `SEMANTAH-E2E-PORTABILITY-V1`. Vorher bleiben Repository, Initiativen und aktive SemantAH-Rollen unverändert.

## Fortschritt

- **T001 ist verifiziert:** Weltgewebe-PR #1485 wurde als Merge-Commit `f00afacc7be4cc551c81c5511faf5f817b04f700` nach grüner CI und zweiachsigem R2-Review gemergt. Der vollständige GitHub-Diff ist an SHA-256 `745483199f2b955a8ac37521445a85f8b9543e92ecf3ff69ed99b3a21ae7554f` gebunden.
- **T002 ist verifiziert:** Weltgewebe-PR #1495 wurde mit attestiertem Head `31ca3c433dcaf3b941c5e1c95167a68e9f68ceb8` und Merge-Commit `adc060cfbb9d055a7b63c494fa042e7c57ca7bea` gemergt. Der kanonische GitHub-Diff ist an SHA-256 `b54ec09ce52fe7e109b18da8f4ed7e5fc5e33783ff75252dd354c605ec6988e7` gebunden; der lokale Binärdiff an `6ffdc08f17d68ecb72a0a4dfe8ade167ab47dabbe6b0cf7a1a35410e4e2e1375`.
- **T003 ist verifiziert:** Weltgewebe-PR #1500 wurde mit Head `35dea9a90cf0bb84f167ed596e3cb5de1423ca6a` und Merge-Commit `bbeb7c63f6ce0a807d0203a7062198a545a2a6a5` gemergt. PostgreSQL 16.14 und pg_trgm 1.6 sind belegt; pgvector ist im gepinnten Image nicht verfügbar und wurde nicht fingiert.
- **T004 ist verifiziert:** PR #1502 wurde mit Head `9fbc592d3301d3c156a931ed18112a76ff55e1da` als `9f44895337b2ecf97f83a125ca30f1247d98745f` gemergt. PR #1506 wurde mit Head `fc710aa4c0d0819d4acaef5b0034f8d88a9eb6b2` als `4c6ef9e8fec0a2b17cc5babb5b0e02798002b89b` gemergt. Die finalen erforderlichen Gates waren grün; frühere fehlgeschlagene oder abgebrochene Review-Evidence-Läufe wurden nicht als Mergebeleg verwendet.
- **T005 ist verifiziert:** PR #1529 ist gemergt; der geprüfte PR-Head und der Merge-Commit besitzen denselben vollständigen Git-Tree. Die Projektion bleibt bei unbekannter Sichtbarkeit fail-closed.
- **T006 ist verifiziert:** PR #1546 wurde mit finalem Head `035611b34a94abfd2f30803358e8b2a70913e0cb` als `059882cd49ee31b0b09815626f374860149174bd` gemergt. Der 73.307-Byte-Binärdiff ist an SHA-256 `0e55964ca9253cf24ba72f93595a54bf5a83af2e4b4ceecdc5282b68335f7cd3` gebunden; geprüfter Head-Tree und Main-Tree sind identisch.
- **T007 ist aktuell:** Die Websuche wird auf die T006-Server-API umgestellt und „Ähnliche Knoten“ wird als getrennte maschinelle Ähnlichkeitsoberfläche integriert.
- **T010/T011 sind Review-Follow-ups:** T010 untersucht einen skalierbaren autorisierten Retrievalpfad jenseits der fail-closed 1000er-Grenze; T011 härtet literale Titelpräfixe gegen unbeabsichtigte `%`-/`_`-LIKE-Wildcards. Beide blockieren T007 nicht.
- **SemantAH bleibt unverändert:** keine Archivierung, keine Runtime-Entfernung und keine Bereinigung vor T008/T009.

## T002-Test- und Reviewbindung

Auf dem veröffentlichten T002-Head bestanden 816 Docmeta-, 191 Agent- und 277 CI-Prüfungen, 27 fokussierte Semantic-Search-Tests sowie Generator-, Struktur-, Shell-, Plattform- und Vertragsgates; 11 Skips waren erwartet. Zwei getrennte R2-Self-Reviews auf den Achsen Correctness und Data Integrity wurden vom repository-eigenen Review-Evidence-Gate akzeptiert. Required Merge Gate, Review Evidence Gate, Web E2E, CodeQL, Docs Guard, PostgreSQL Integration Proofs, Cloudflare Pages und Vercel waren vor Merge grün.

T002 implementiert keine PostgreSQL-Migration, Search-API, Worker-, Web- oder Deploymentfunktion. Der Merge veröffentlicht Architektur-, Benchmark- und Testbelege, aber keine produktive semantische Suche.

## T003-PostgreSQL-, Projektions- und Betriebsbeleg

PR #1500 veröffentlichte einen ausführbaren Suchprojektionsvertrag unter `contracts/search`, aber bewusst keine automatisch ausgerollte SQLx-Migration. Der Merge verändert daher keine Produktionsdatenbank. Der Beleg lief lokal und in GitHub CI gegen PostgreSQL 16.14 und pg_trgm 1.6. `pgvector` war im gepinnten Image nicht verfügbar; T003 stoppt deshalb vor Vektorpersistenz und ANN-Indizes.

Der reale FTS-/Trigramm-Pfad erreichte auf dem synthetischen T002-Goldset 14/19 natürliche Top-3-Treffer, 0 falsche Top-1 und 0 Sichtbarkeitslecks. Sichtbarkeit, Löschstatus, aktive Generation, explizite Autorisierung und Filter werden vor dem Ranking erzwungen. Deterministischer Neuaufbau und `pg_dump`/`pg_restore` erzeugten identische Projektions-Digests.

Der GitHub-Diff ist an SHA-256 `573cff9cabb87cd3f43f8a5d2ef8ba03f6f90a596fc7c4ae97f8966d3373bcd2` gebunden. Zwei R3-Berichte sowie alle Required-Gates waren grün. Der gemergte Main-Tree ist für alle zehn T003-Dateien identisch zum geprüften Head.

T003 belegt keine Produktions-PITR-Laufzeit, reale Nutzerrelevanz, Produktionslatenz, pgvector-Paketierung, Worker, API, Websuche oder öffentlichen semantischen Livezustand.

## Aktuelle öffentliche Produktionsprüfung

Der erste T002-Readback unmittelbar nach Merge beobachtete noch den vorangehenden Live-Commit `d9a5377a07e4e9728778f327fa87668945d007cf`. Beim erneuten Bureau-Closeout-Readback am 19. Juli 2026 bestanden DNS für Root, WWW und API, HTTP→HTTPS, beide HTTPS-Roots, Map-Route, API-Readiness, Version-JSON, lokalen Basemap-Stil, Glyphen sowie stabile und versionierte Hamburg- und Schleswig-Holstein-PMTiles. Der dabei öffentlich gelesene Live-Commit war `b5a9383fc36b381bf5a68fd2e9a287d13f2caa82`; Git bestätigt den T002-Merge-Commit `adc060cfbb9d055a7b63c494fa042e7c57ca7bea` als dessen Vorfahren.

Das belegt, dass der T002-Quellstand inzwischen in der ausgelieferten Commitlinie enthalten ist und die bestehende öffentliche Oberfläche gesund ist. Es belegt weiterhin keine produktive semantische Suche: PR #1495 besitzt keinen Runtime- oder Deployschnitt. Ein semantischer öffentlicher Live-Beweis bleibt T008 vorbehalten.

## Stopbedingungen

Die Initiative wird gestoppt oder neu geschnitten, wenn Sichtbarkeit nicht vor Retrieval durchsetzbar ist, PostgreSQL keine tragfähige Betriebsoption für die Projektion bietet, der lokale Modellpfad die Qualitätsgrenzen klar verfehlt und keine ausdrückliche Kostenfreigabe besteht, oder der erwartete Relevanzgewinn gegenüber FTS/Trigrammen praktisch unbedeutend bleibt.
