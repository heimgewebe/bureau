# Cabinet Ecosystem Intelligence Masterplan v1 — Bureau Anschluss

## Status

- Typ: Bureau-Anschlussnotiz zu Cabinet-Masterplan
- Stand: 2026-06-30
- Quelle: `/home/alex/repos/cabinet/docs/blueprints/ecosystem-intelligence-masterplan-v1.md`
- Bureau-Rolle: operative Taktung, Queue, Delegation, Rueckmeldung
- Autonomie: nicht aktiviert

## Zweck

Cabinet soll zur Ecosystem-Intelligence-Schicht fuer das Heimgewebe-Repooekosystem werden. Bureau soll nicht durch Cabinet ersetzt werden. Bureau wird der operative Taktgeber, der aus validierten Cabinet-Kandidaten Arbeit ziehen kann und seine Ergebnisse wieder an Cabinet berichtet.

Kurzform:

```text
Cabinet denkt, ordnet, belegt, priorisiert und lernt.
Bureau taktet, delegiert und meldet belegte Ergebnisse zurueck.
Agenten patchen, analysieren oder reviewen im Auftrag.
Git, CI, Runtime und Contracts pruefen die Realitaet.
```

## Bureau-Leitplanken

1. Bureau liest nur validierte oder explizit freigegebene Cabinet-Kandidaten.
2. Bureau interpretiert Cabinet-Scores nicht als Wahrheit.
3. Bureau schreibt keine Entscheidungen in Cabinet.
4. Bureau darf Laufberichte und Befunde append-only an Cabinet liefern, sobald eine Bridge existiert.
5. Bureau darf Agenten nur mit Zielbeleg, Scope und Rueckmeldevertrag beauftragen.
6. Bureau darf `done` nicht aus bloss vorhandener Evidence ableiten; Evidence muss bestanden, akzeptiert oder begruendet nicht anwendbar sein.

## Ziel-Schnittstellen

### Cabinet -> Bureau

Geplanter Export:

```text
.cabinet-state/bureau/frontier.json
```

Inhalt:

```yaml
generated_at:
tasks:
  - id:
    source_path:
    repo:
    priority:
    risk:
    status: approved
    target_proof:
    acceptance:
    briefing_profile:
```

Nur Tasks mit `status: approved` sind fuer Bureau operational relevant.

### Bureau -> Cabinet

Geplanter Rueckweg:

```text
pruefung/10 Laeufe/bureau-run-*.md
pruefung/30 Befunde/bureau-*.md
```

Minimaler Laufbericht:

```yaml
task_id:
repo:
branch:
base_sha:
head_sha:
agent:
model:
commands_run:
tests:
ci:
result:
open_risks:
rollback:
next_action:
```

## Phasen fuer Bureau

### B0: Kenntnisnahme

Diese Datei existiert im Bureau-Repo. Keine Logik ist aktiv.

Akzeptanz:

- Plan ist versioniert oder bewusst als lokaler Arbeitsstand markiert.
- Kein Timer liest Cabinet automatisch.
- Kein Agent wird automatisch aus Cabinet gestartet.

### B1: Read-only Frontier Reader

Bureau bekommt einen Reader fuer Cabinet-Frontier-Artefakte.

Geplante Dateien:

```text
src/bureau/cabinet_frontier.py
tests/test_cabinet_frontier.py
```

Akzeptanz:

- Reader akzeptiert nur JSON nach Schema.
- Unknown fields werden konservativ behandelt.
- Nicht-approved Tasks werden ignoriert.
- Fehler erzeugen Report, keine stille Korrektur.

### B2: Task Import Candidate

Bureau kann approved Cabinet-Kandidaten als eigene Bureau-Tasks vormerken.

Akzeptanz:

- Import ist idempotent.
- Quelle bleibt sichtbar.
- Bureau darf Prioritaet senken, aber nicht ohne Beleg erhoehen.
- Menschliche Freigabe bleibt bei risk >= high erforderlich.

### B3: Run Reporter

Bureau schreibt standardisierte Ergebnisreports, die Cabinet importieren kann.

Akzeptanz:

- `done` braucht bestandene Evidence oder `not_applicable_with_reason`.
- `blocked` nennt Blocker und naechste diagnostische Aktion.
- `failed` bleibt erhalten und wird nicht automatisch ueberschrieben.

### B4: Agent Routing

Bureau nutzt Cabinet-Briefings fuer Agentenrouting.

Routing-Hypothese:

- Jules: kleiner Patch
- Codex: Review, Invariante, Sicherheitslogik
- Claude/Opus: Architektur, grosse Refactors
- lokale Agenten: billige Vorpruefung

Akzeptanz:

- Jeder Agentenauftrag enthaelt allowed_scope, forbidden_changes, acceptance_tests und output_contract.
- Kein Agentenauftrag ohne source_task_id.

### B5: Feedback Loop

Bureau liefert wiederkehrende Fehlerklassen an Cabinet zurueck.

Beispiele:

- fehlende Akzeptanzkriterien
- CI gruen, aber lokale Evidence fehlt
- Scope creep
- stale PRs
- wiederholte Tool-Blockaden

Akzeptanz:

- Bureau schlaegt keine Regeln direkt als aktiv vor.
- Regelvorschlaege gehen an Cabinet zur Pruefung.

## Naechste Bureau-Aktion

Erster sinnvoller Bureau-Task:

```text
BUR-CAB-001: Read-only Cabinet Frontier Reader entwerfen
```

Nicht sofort implementieren, solange Cabinet `frontier.json` und Schemas noch nicht bereitstellt. Vorher nur vorbereiten:

- erwartetes JSON-Contract-Dokument lesen
- Parser-Grenzen definieren
- Failure Modes beschreiben
- Tests fuer approved/ignored/invalid entwerfen

## Aktuelle Leerstelle

Der lokale Bureau-Checkout war beim Anlegen dieser Datei nicht sauber synchron: `main` war lokal voraus und hinter `origin/main`, ausserdem existierte eine ungetrackte Datei. Deshalb sollte diese Anschlussnotiz vor Merge/PR gegen den aktuellen Bureau-Stand reconciled werden.

## Kurzform

Bureau wird nicht zum Gehirn des Systems. Bureau wird die Hand mit Uhr. Cabinet wird die Schicht, die weiss, warum die Hand etwas tun soll, woran Erfolg gemessen wird und was aus Fehlern gelernt wurde.
