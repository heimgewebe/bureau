# RepoBrief Frontdoor and Grounding Verifier v1

Registered: 2026-07-09

## Ziel

RepoBrief soll für Agenten von einer umfangreichen Bundle-Fläche zu einer nutzbaren
Frontdoor werden: ein Agent stellt eine Repo-Frage, erhält ein kleines task-passendes
Context Pack mit auflösbaren Belegen, und eine separate Prüfung kann nachträglich
feststellen, ob die Antwort ihre angegebenen Citations und Ranges technisch trägt.

Kurzform:

- `repobrief ask` / `lens ask` baut ein belegtes Context Pack;
- `repobrief verify-answer` prüft deklarierte Citations/Ranges gegen vorhandene Snapshots;
- MCP verteilt dieselben read-only Flächen später an andere Agenten;
- Delta/Freshness und History Lens kommen erst nach stabiler lokaler Semantik.

## Vorprüfung

### These

RepoBriefs Differenzierungshebel ist nicht mehr der nächste größere Dump, sondern die
Überprüfbarkeit von Repo-Aussagen. Agenten brauchen weniger Artefaktwissen und mehr einen
Frontdoor-Befehl, der Frage, Task-Profil, Tokenbudget, Required Reading, Freshness und
Citations zusammenführt.

### Antithese

Ein Verifier kann leicht als Wahrheitsdetektor missverstanden werden. Technisch prüfbar ist
zunächst nur: Citation existiert, Range löst auf, Hash/Text stimmt, Pflichtdeklarationen
fehlen nicht. Semantische Wahrheit, Vollständigkeit, Runtime-Korrektheit und
Merge-Sicherheit bleiben Nicht-Claims.

### Synthese

Die Initiative koppelt Frontdoor und Grounding-Verifier. `ask` macht RepoBrief praktisch
nutzbar; `verify-answer` macht die Nutzung prüfbar. Beides bleibt read-only und baut auf
bestehenden RepoBrief-Flächen auf.

## Alternative Sinnachse

Wenn Nutzen im Alltag maximiert wird, beginnt man mit `ask`. Wenn Alleinstellungsmerkmal
maximiert wird, beginnt man mit dem Grounding-Verifier. Wenn Distribution maximiert wird,
kommt MCP früher. Diese Initiative entscheidet: Vertrag und Verifier zuerst klein schneiden,
Ask direkt danach, MCP erst nach stabiler CLI/Core-Semantik.

## Bezug zu bestehenden RepoBrief-Aufgaben

Diese Initiative ersetzt keine bestehenden RBV1/RBAW-Slices. Sie bündelt sie in eine
agententaugliche Arbeitskette:

- Health/Freshness/Availability und CLI-Alias sind Grundlage.
- Required Reading und Agent Consumption Preflight liefern Pflichtlese-Regeln.
- Retrieval/Relation/Graph/Symbol-Slices liefern bessere Auswahlflächen.
- MCP Boundary bleibt die Distributionsgrenze.
- Patch Evaluation Sidecar bleibt extern; RepoBrief Core erhält keine Mutation.

## Umsetzungskette

### T001 — Grounding Verifier contract

Definiert `answer_declaration` und `grounding_verdict` als Contract-/Docs-/Fixture-Slice.
Das Verdict bewertet technische Grounding-Bedingungen, keine Wahrheit.

### T002 — Minimal citation/range verifier core

Implementiert einen deterministischen read-only Core: Snapshot/Stem prüfen, Citation Map
lesen, Citation-IDs und Ranges auflösen, Hash/Text-Drift melden, missing-required als
`fail`, missing-recommended als `warn`.

### T003 — Answer compliance integration

Bindet Required Reading / Agent Consumption Trace an das Grounding Verdict. Task-Profile
bestimmen Pflichtartefakte; Non-Claims und Freshness Caveats werden gespiegelt.

### T004 — Ask frontdoor contract

Spezifiziert Request, Context Pack und Answer Scaffold für `repobrief ask` / `lens ask`:
Query, Task-Profil, Tokenbudget, Snapshot-Policy, Required Reading und Output-Verpflichtung.

### T005 — Minimal `repobrief ask` CLI prototype

Ein kalter Agent kann ein kleines, zitierbares Context Pack erzeugen, ohne Git, Refresh,
Snapshot-Erzeugung oder Mutation auszulösen.

### T006 — Ask gold-query evaluation

Misst, ob Ask-Context-Packs bessere Evidenz liefern: Citation Coverage, Required Reading
Coverage, Expected Path/Range Recall, MRR@10, Missing Evidence Taxonomy und Budget-Rate.

### T007 — MCP read-only frontdoor

Exponiert Ask Context und Grounding Verify als MCP read-only Flächen. Lesezugriffe lösen
kein `snapshot_create` aus.

### T008 — Delta/freshness invalidation v1

Prüft alte Answer Declarations gegen neuere Snapshots und markiert Citations als `valid`,
`drifted`, `missing` oder `not_comparable`.

### T009 — History Lens derived navigation

Optionales Derived-Artefakt für Churn, Blame-Verdichtung und Commit-/PR-Provenance-Ketten.
Keine Schuld-, Ownership-, Korrektheits- oder Vollständigkeitsurteile.

## Nicht-Ziele

- kein Wahrheitsdetektor;
- keine automatische Claim-Bewertung `supported/unsupported` in v1;
- keine Patch-, Shell-, Test-, PR- oder Merge-Autorität in RepoBrief Core;
- kein automatischer Snapshot-Refresh durch Lesen;
- kein LLM-Reranking als Kernpflicht;
- keine Package-/Repo-Umbenennung;
- keine History-Vollindizierung als Startbedingung.

## Risiken und Gegenmaßnahmen

| Risiko | Folge | Gegenmaßnahme |
| --- | --- | --- |
| Verifier wird als Wahrheitsdetektor gelesen | falsche Sicherheit | Name Grounding Verifier, Pflicht-Non-Claims, keine `true/false`-Claims |
| Ask erzeugt zu viel Kontext | Agent liest wieder linear | Tokenbudget, Task-Profile, Gold Query Evaluation |
| MCP vor stabiler Semantik | Client-Drift | MCP nach CLI/Core-Vertrag |
| Delta-Citations werden überdehnt | alte Belege wirken gültiger als sie sind | explizite Invalidation, `not_comparable` zulassen |
| History Lens erzeugt Bias/Privacy-Risiko | falsche Blame-/Ownership-Deutung | Profil-Gate, Export Safety, keine Personenurteile |

## Akzeptanz der Initiative

Die Initiative ist abgeschlossen, wenn:

- Grounding-Verifier-Verträge und minimaler Verifier-Core existieren;
- `repobrief ask` / `lens ask` ein kleines read-only Context Pack erzeugen kann;
- Ask-Evaluation eine messbare Baseline liefert;
- MCP read-only Frontdoor auf stabiler Semantik steht;
- Delta/Freshness-Invalidation und History Lens entweder umgesetzt oder mit belegter
  Stop-/Defer-Entscheidung geschlossen sind;
- alle Outputs ihre Nicht-Claims sichtbar tragen.
