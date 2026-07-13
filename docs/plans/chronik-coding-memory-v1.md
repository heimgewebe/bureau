# Chronik Coding Memory v1

Stand: 2026-07-13

## Entscheidung

Chronik wird als lokales, append-only Arbeitsgedächtnis für Coding- und Operator-Outcomes produktivisiert. Grabowski schreibt explizit zielgebundene Ereignisse, importiert sie ohne Plexer direkt und liest Historie ausschließlich als `historical_only`. Vibe-Lab prüft prospektiv, ob ein eingefrorener Historienbrief reale Coding-Entscheidungen verbessert. Bureau und der Operator entscheiden über jede spätere Übernahme.

## Rollen

- **Grabowski:** Ausführung, Work Context, Outbox, Live-Preflight und Evidence-Receipts.
- **Chronik:** idempotenter lokaler Import, append-only Historie, begrenzte Abfrage und hashgebundener Cohort-Receipt.
- **Vibe-Lab:** vorab registrierter Kontroll-/Behandlungsvergleich, unabhängige Bewertung und geprüfte Abschlussentscheidung.
- **Bureau:** Aufgaben- und Promotionentscheidung; keine automatische Übernahme.
- **Systemkatalog:** kanonische Rollen und Beziehungen.

Plexer, Heimlern-Runtime und Leitstand sind keine Voraussetzung dieses Pfads.

## Reihenfolge

1. Chronik vereinheitlicht API- und CLI-Envelopes und bietet lokalen idempotenten Import, Query und Freeze.
2. Grabowski erfasst `repo`, `component`, `operation`, optionale Bureau-/PR-Referenzen und bietet Import sowie read-only History.
3. Vibe-Lab registriert den Chronik-History-Brief-Effekt vor jeder Beobachtung.
4. Systemkatalog verschiebt die aktive Lernrolle von Heimlern zu Vibe-Lab.
5. Mindestens drei natürliche Kontroll- und drei natürliche Behandlungsfälle werden unabhängig und möglichst blind bewertet.

## Harte Grenzen

- Chronik blockiert keine Coding-Ausführung.
- Historie ersetzt niemals Git-, GitHub-, CI- oder Runtime-Readback.
- Keine Rohlogs, Prompts, Secrets oder vollständigen Diffs im Ledger.
- Kein automatisches Routing, keine automatische Policy und keine automatische Bureau-Aufgabe.
- Keine retrospektiv erfundenen Messfälle und keine produktive Wiederholungsmutation nur für das Experiment.

## Erfolg und Exit

Technischer Erfolg: Ein zielgebundenes Grabowski-Ereignis wird genau einmal importiert, gezielt gelesen und als kohärente, hashgebundene Stichprobe eingefroren.

Produktiver Erfolg: Der registrierte Vibe-Lab-Vergleich zeigt einen materiellen Entscheidungsgewinn ohne `history_as_live_truth`-Fehler und bei vertretbarem Zusatzaufwand.

Exit: Wenn nach dem registrierten Mindestvergleich kein materieller Nutzen erkennbar ist oder Historie falsche Sicherheit erzeugt, bleibt Chronik als enger optionaler Ledgerpfad und der History-Brief wird nicht gefördert.
