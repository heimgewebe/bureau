# Bureau Repo-Audit 2026-07

Status: umgesetzt (Sofortmaßnahmen) / offen (Board-Tasks)
Scope: kompletter Repository-Stand auf `main` (a19c6f0), Code, Schemas, Registry, Docs, CI, systemd
Methode: `make validate` (ruff, pytest, `bureau check`), vollständige Quelltext-Lektüre aller
Module, Abgleich Docs↔Code↔Registry, Laufzeitprüfung von `doctor`/`lifecycle` gegen einen
leeren State-Root.

## 1. Ausgangslage

- Lint sauber, 157 Tests grün, `bureau check` valide.
- Die Substanz der Befunde liegt daher nicht in kaputten Tests, sondern in totem/irreführendem
  Code, Doku-Drift, Packaging-Lücken und Board-Inkonsistenzen, die `doctor` als unhealthy meldet.

## 2. Befunde und Maßnahmen

### 2.1 In diesem Commit behoben

| # | Befund | Ort | Maßnahme |
|---|---|---|---|
| F1 | `_requires_agent_brief` endete auf `... or True` und gab bei aktivem Policy-Flag plus erkanntem Agent-Profil immer `True` zurück; der vorangehende `task.mode`-Zweig war tot (Profil war dort nie `None`), der `dispatch`-Parameter wirkungslos. | `src/bureau/v2.py` | Funktion auf die tatsächliche Semantik reduziert (Policy verlangt Brief ∧ externes Agent-Profil erkannt ⇒ Brief nötig); toter Zweig, tote Oder-Kette und unbenutzter Parameter entfernt. Verhalten unverändert, Tests decken beide Pfade. |
| F2 | No-op `degraded = False` bei High-Severity-Bottlenecks: `degraded` war an dieser Stelle immer schon `False`; die Zeile suggerierte eine Entscheidung, die nie stattfand. | `src/bureau/agent_frontier.py` | Tote Anweisung entfernt. Semantik bleibt: Bottlenecks sind Befunde des Reports, keine Degradierung des Governors. |
| F3 | Hartkodierter maschinenspezifischer Pfad `/home/alex/repos/weltgewebe` als `working_repository` im Promotion-Kandidaten. | `src/bureau/weltgewebe_source.py` | Auf `Path.home() / "repos/weltgewebe"` umgestellt, konsistent mit den übrigen Defaults (`grabowski_adapter`, `closure`). |
| F4 | `cycle_contract` besitzt eine vollwertige CLI (`prog="bureau-cycle"`: `cycle-id`, `begin`, `validate`, `attention`), war aber nicht als Konsolen-Skript installierbar. | `pyproject.toml` | Entry Point `bureau-cycle = "bureau.cycle_contract:main"` ergänzt. |
| F5 | Der Discovery-Scanner hatte keinen installierbaren Einstieg; nur `bureau-closure`/`bureau-closure-runner` folgten dem Muster „Kern + gehärteter Runner“. | `pyproject.toml` | Entry Point `bureau-discovery = "bureau.discovery_runner:main"` ergänzt (Runner schreibt bei Crash ein terminales Failed-Receipt). |
| F6 | `.gitignore` kannte `.source/` nicht; der Sync-Workflow schützt sich nur über `.git/info/exclude`, lokale Reproduktion des Workflows verschmutzte den Tree. | `.gitignore` | `.source/` ergänzt. |
| F7 | Doku-Drift: `ops/systemd/bureau-agent-frontier.*` und `bureau-codex-bridge.*` erwarten Wrapper unter `~/.local/libexec/`, ohne dass irgendeine Doku Installation oder Wrapper beschreibt; Closure-Planner/Frontier/Codex-Bridge fehlten in `docs/operations.md` komplett. | `docs/operations.md` | Installations- und Betriebsabschnitte für Closure-Planner, Agent-Frontier-Governor und Codex-Bridge ergänzt (inkl. libexec-Wrapper). |
| F8 | README nannte nur den `bureau`-Kern; die sechs Begleit-CLIs und ihre Rollen waren unauffindbar. | `README.md` | Abschnitt „Companion commands“ ergänzt. |
| F9 | Audit-Ergebnis war nirgends versioniert; Folgearbeiten hatten keinen Board-Anker. | `docs/reports/`, `registry/` | Dieses Dokument plus Initiative `BUR-2026-003` mit expliziten Folge-Tasks. |

### 2.2 Als Board-Tasks registriert (Initiative BUR-2026-003)

Diese Punkte verlangen Entscheidungen des Betreibers oder Arbeit außerhalb dieses Checkouts und
werden bewusst nicht still „mitgefixt“:

- **T001 — Lifecycle von BUR-2026-001 reconciligen.** Initiative ist `completed/completed`,
  aber `BUR-2026-001-T006` und `-T009` stehen auf `planned`. `doctor`/`lifecycle` melden
  deshalb dauerhaft `reopen-required` und `healthy: false`. Die Closure-Bridge erlaubt solche
  Tasks zwar gezielt (lokaler Closure-Plan + gültiger Brief), die Runtime-Truth bleibt aber
  widersprüchlich. Entscheidung nötig: Tasks verifizieren, in eine aktive Initiative umhängen
  oder Initiative regulär wiedereröffnen.
- **T002 — Queue-Lanes mit Task-Readiness abgleichen.** Alle fünf Queue-Einträge
  (`BUR-2026-002-T001…T005`) stehen auf `planned`; `doctor` erzeugt für jeden einen
  `queue_finding`. Entweder Tasks bewusst auf `ready` heben, sobald sie es sind, oder die Queue
  auf tatsächlich bereitstehende Arbeit reduzieren.
- **T003 — Abbauplan für `legacy.py` festlegen.** Das v0.1-Modul wird nur noch für Migration
  und Kontrakt-Kompatibilität mitgeschleppt, trägt Ruff-Ausnahmen, ein bekanntes
  Verbindungsleck in `StateStore._initialize` (Connection wird nie geschlossen) und semantisch
  überholte Varianten von `complete_run`/`reconcile`. Zielbild und Frist für die Entfernung
  bzw. das Einfrieren definieren.

### 2.3 Beobachtet, bewusst unverändert

- `bureau-agent-scout` bleibt als Alias-Entry-Point auf `agent_frontier:main` bestehen
  (vermutlich absichtliche Zweitbenennung).
- `schemas/agent-frontier-report.v1.schema.json` wird nur in Tests erzwungen, nicht zur
  Laufzeit. Das ist als Test-Kontrakt vertretbar; Laufzeitvalidierung wäre eine Option, kostet
  aber pro Zyklus Schema-Kompilierung.
- Registry-Ressourcen (`registry/resources/*.json`) und historische Task-/Receipt-Artefakte
  enthalten `/home/alex/…`-Pfade. Das sind Betriebsdaten des realen Hosts, keine Bibliotheks-
  Defaults; sie bleiben unangetastet.
- `work/grabowski-safety-smooth-ops/` ist ein bewusst versionierter Arbeitsbereich mit
  Receipts; kein Handlungsbedarf.
- CI-Matrix (3.10, 3.12) deckt die deklarierte Untergrenze und eine aktuelle Version ab;
  3.13-Erweiterung optional.

## 3. Validierung

- `make validate` nach Umsetzung: Lint sauber, alle Tests grün, `bureau check` valide
  (inkl. der neuen Registry-Dateien für BUR-2026-003).
- `lifecycle` bleibt für BUR-2026-001 absichtlich inkonsistent, bis T001 entschieden ist;
  BUR-2026-003 selbst ist konsistent (`active` mit offenen Tasks).
