# Bureau Control Plane v3: Autoritäts- und Consumerinventar

## Zweck

`python -m bureau.authority_inventory` erzeugt eine begrenzte, maschinenlesbare und strikt read-only Sicht auf die heutigen Bureau-Autoritäten und Consumer.

Der Inventarlauf verbindet vier Belegklassen:

1. statische Python-Analyse für Registry-, StateStore-, GitHub- und Grabowski-Zugriffe;
2. Workflowanalyse für GitHub-Transport- und Registry-Schreibkandidaten;
3. systemd-Unit-Inventar einschließlich optionalem read-only Live-Readback;
4. SQLite-Readback mit `mode=ro`, `query_only`, Integritätsprüfung, Schema und begrenzten Tabellenzählungen.

## Aufruf

```bash
python -m bureau.authority_inventory \
  --root /pfad/zum/bureau-release \
  --state-root ~/.local/state/bureau \
  --json
```

Für hermetische Tests oder Hosts ohne systemd-Userbus kann der Live-Readback mit `--skip-systemd` abgeschaltet werden. Das wird im Ergebnis sichtbar; fehlende Live-Evidence wird nicht als Gesundheit interpretiert.

## Ausgabe

Jeder Consumer nennt:

- Quellpfad oder externe Vertragsidentität,
- gelesene Autoritätsflächen,
- mögliche Schreibflächen,
- statische Evidenzmarker,
- Zielautorität unter Control Plane v3,
- Migrationsdisposition.

Besonders sichtbar bleiben:

- heutige Doppelwriter für Git Registry und StateStore,
- operative Git-Schreiber, die nach dem Cutover entfernt werden müssen,
- GitHub-Transporte, die nur Code, CI oder redigierte Snapshots transportieren dürfen,
- StateStore-Schreiber, die in den Single-Writer-Vertrag konvergieren müssen.

## Grenzen

Die Oberfläche erteilt keine Mutation, klassifiziert keine fremde Arbeit als übernehmbar und beweist bei fehlendem Live-Systemd-Readback keine Runtimegesundheit. Ein statischer Schreibkandidat ist ein zu prüfender Consumer, nicht automatisch ein Fehler oder eine Löschfreigabe.

Der Inventarhash bindet den vollständigen beobachteten Inhalt. Ein späterer Apply- oder Cutover-Vertrag muss den Hash frisch lesen und separat autorisieren.
