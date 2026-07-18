# RepoGround Agent Utility v1

## Zweck

RepoGround wird als kleine, read-only und commit-gebundene Evidenz- und Kontextkomponente unter Grabowski genutzt. Native Live-Werkzeuge bleiben Standard, solange kein aufgabenbezogener Zusatznutzen gemessen ist.

## Authority

- **Bureau:** Plan, Reihenfolge und Taskzustand.
- **GitHub:** Commit-, Pull-Request-, CI- und Merge-Fakten.
- **Weltgewebe:** repo-nahe Regeln, Contracts und fachliche Wahrheit.
- **Grabowski:** Livezustand, Operationen und Runtimebelege.
- **RepoGround:** diagnostische Navigation, Evidenzadressen und Änderungskontext.

RepoGround etabliert weder Wahrheit noch Vollständigkeit, Patchkorrektheit, Merge-Reife oder Runtime-Verhalten.

## Reihenfolge

### T001 — Publikationswahrheit

Alle Grabowski-Consumer werden an dieselbe validierte Latest-Complete-Publikation unter `manifest-publications` gebunden. Der alte `merges`-Bestand ist nur ein sichtbarer historischer Fallback niedrigerer Authority. Freshness trennt publizierten Basiscommit, lokalen HEAD und Dirty Overlay.

### T002 — dünner Composer

Bestehende RepoGround-Flächen werden komponiert: Agent Impact Context, Query Context Bundle, PR Delta Cards, Entry Manifest, Symbol-/Call-Graph, Citation- und Live-Evidenz. Eingaben sind Repo, Basis/Ziel oder gebundener Diff, Aufgabenklasse und Kontextbudget. Es entsteht keine neue Grundvertragsschicht.

### T003 — Weltgewebe-Pilot

Eine einzige Change Capsule wird an drei Klassen geprüft: Datenbank/Auth, Web/Karte und Deployment/Kubernetes. Jeder Goldfall besitzt eine gepaarte Baseline ohne Kapsel. Das Gate verlangt keine Qualitätsregression und in mindestens zwei Fällen ein zusätzlich korrekt erkanntes kritisches Ziel oder mindestens 20 Prozent weniger Toolaufrufe, Tokens, Zeit oder Korrekturschleifen.

### T004 — bedingte Generalisierung

Nur nach bestandenem T003-Gate werden bewährte repo-unabhängige Teile extrahiert. Der Startumfang bleibt auf `change_impact`, `find_relevant_tests` und `ground_claim` begrenzt und wird auf RepoGround sowie einem weiteren aktiven Repository geprüft.

## Harte Gates

1. Exakte Commit-, Manifest- und Diff-Bindung.
2. Deterministische Auswahl und Ausgabe.
3. Sichtbare Authority und negative Semantik.
4. Keine Read-seitige Mutation von Registry oder Bundles.
5. Externes vollständiges Diff-Artefakt, Head-/Diff-SHA-256-Review und grüne CI vor jedem nichttrivialen Merge.
6. Maximal ein aktiver Task dieser Initiative.
7. Keine Default-Promotion ohne gepaarten Nutzennachweis.

## Messgrößen

- korrekt erkannte kritische Ziele;
- fehlende und irrelevante Ziele;
- relevante Test- und Gate-Auswahl;
- Toolaufrufe und Korrekturschleifen;
- Kontextbytes beziehungsweise Tokens;
- Zeit und Kosten;
- falsche Freshness-, Authority- oder Vollständigkeitsbehauptungen.

## Nichtziele

- universelle Suche oder zweites Repository-Gehirn;
- neue Task-, Claim-, Status- oder Agent-Control-Plane;
- Embedding-first- oder semantische Defaultsuche;
- Cross-Repo-Föderation;
- Vollrepo-Dumps im Agentenprompt;
- automatische Patches, Issues, Commits, Merges oder Deployments durch RepoGround;
- weitere Karten-, Lens- oder Authority-Verträge ohne produktiven Consumer.

## Geparkte Arbeit

Die GitHub-Aufgaben #637, #638 und #642 zu Audit-Lane-Runner, Vergleich und Routingkalibrierung bleiben technisch gültig, aber bis nach dem Weltgewebe-Pilot geparkt. Sie optimieren Spezialreviews, bevor der wichtigere reale Produktnutzen belegt ist.

## GitHub-Bezug

Die Issues #681 bis #684 dokumentieren Analyse und Arbeitspakete auf GitHub. Sie sind Verweise, nicht die Bureau-Wahrheit; diese liegt in dieser Initiative und ihren Tasks.
