# RCGA Live-Bundle-Kalibrierung – Receipt 2026-07-13

## Bindung

- Bureau-Task: `RCGA-V1-T002`
- temporärer Draft-PR: `heimgewebe/bureau#476` – geschlossen, nicht gemergt
- Workflow-Run: `29228464312`
- Mess-Head: `8942fbad2d2e08a968d5bfefea33c0f9f5df0a27`
- Actions-Artefakt: `8270657335`
- Actions-Artefakt-Digest: `sha256:7d4007c62295519ac1bae25158155f807fdd948294693e6c19526ded6adb9617`
- Goldset: `docs/evidence/rcga-live-goldset-20260713.json`
- Goldset SHA-256: `7c7e359ccddf539db1e639c7327f878ab8978dec7ff533d5c0733cd5442bf124`
- Evaluation: `docs/evidence/rcga-live-evaluation-20260713.json`
- Evaluation SHA-256: `3f080feb83b23242f7a91e7ae7eba1cee6fb3bed079b6672301df046078f7dc2`
- Rohbeobachtungen SHA-256: `a5ae88e56a2201145164b6ac476d9d49ec7f1f9443850efdcea2fb69e7481277`

## Exakte Repositories

| Repository | Commit | Manifest SHA-256 | Run-ID | Canonical-Digest |
|---|---|---|---|---|
| `heimgewebe/lenskit` | `456d37bd142349bc0c04925d87934eefbbc546ac` | `9f50cdfc8db9452d5d3ba32e46573b682558f08870ee48ce24bf35ff3a5ce897` | `lenskit-full-max-260713-0616` | `1ad2abea7e17b4a79a7ed84675192f55b607f1c6c15c538381edbd9f9cb87363` |
| `heimgewebe/grabowski` | `f6eed48752fd2cf32f070dc69b2112e2498872cb` | `4e49b3d4b3461b787bdd02ce3d0effabc17d3b126b462e929028beabed46e29c` | `grabowski-full-max-260713-0617` | `092137a2035e19938ae6d2a8a3d30826c6a9524a21b861be7141bbc9aca397c6` |
| `heimgewebe/weltgewebe` | `e095903bb71c937d861fa64d7e8a6b593062ca6f` | `33d08b7ef10172fd14efb9c8a7b4c39b4c95159ba4f9252b0659ecd433b90e91` | `weltgewebe-full-max-260713-0617` | `627f32508265fa5af8f2c66ea4e13fcbb16e773a5acef64b9ee71f057e83c9ef` |

## Messbedingungen

Das Goldset wurde vor dem finalen Lauf festgelegt. Baseline und Impact-Fläche erhielten dieselbe Pfadabfrage. Gemessen wurden erwartete Testpfade, Kontextpfadanzahl, Latenz, Quellenstatus, Bundlekohärenz und bytegleiche Wiederholung der Impact-Ausgabe.

Alle drei Bundles waren kohärent. Alle Kernartefakte waren verfügbar. Zwei Wiederholungen je Fall waren gleich. Die drei Quellrepositories blieben nach Bundle-Erzeugung und Abfragen `git status --porcelain`-sauber.

## Ergebnis

- Baseline Target Recall: `1.0`
- Impact Target Recall: `0.6666666666666666`
- Delta: `-0.33333333333333337`
- No-case-regression: `false`
- Default-Promotion: `false`
- Empfehlung: `do_not_promote_refine_or_remove`

### Fälle

- Lenskit: Recall `1.0 → 1.0`; Kontextpfade `11 → 7`; Latenz `1022.95 ms → 154.57 ms`.
- Grabowski: Recall `1.0 → 0.0`; Kontextpfade `11 → 7`; der echte Test `tests/test_job_finalizer.py` fehlt, während nur konventionell geratene Pfade ausgegeben werden.
- Weltgewebe: Recall `1.0 → 1.0`; Kontextpfade `10 → 8`; Latenz `58.46 ms → 83.38 ms`.

## Belegt / plausibel / nicht belegt

**Belegt:** Die aktuelle Impact-Fläche ist auf diesem festen Live-Goldset keine Verbesserung und darf nicht standardmäßig aktiviert werden. Sie komprimiert den Kontext, verfehlt aber einen zentralen Testpfad.

**Plausibel:** Die Kompression ist nützlich, wenn Retrieval-belegte Testpfade als eigene Evidenzklasse übernommen werden und Recall nicht regressiert.

**Nicht belegt:** allgemeine Agentenverbesserung, vollständige Call- oder Testbeziehungen, Antwortkorrektheit, Reviewvollständigkeit, Merge-Reife oder Standardbeförderung.

## Reparaturbedarf

1. Testpfade aus dem bereits aufgelösten Query-Kontext als `resolved_query` übernehmen, ohne sie zu Graphkanten oder Coverage hochzustufen.
2. Leere Pfade aus der Evaluationsprojektion entfernen.
3. Bei recall-gleicher Ausgabe die registrierte Achse Kontextkompression messen; eine Beförderung bleibt danach dennoch eine getrennte Entscheidung.
